from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import qrcode
from astrbot.api import logger
from yarl import URL

from .helper_utils import cfg, clean_text, read_bool, read_int

QR_GENERATE_ENDPOINT = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_ENDPOINT = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILIBILI_COOKIE_URL = "https://www.bilibili.com/"
SAVED_CREDENTIALS_FILENAME = "bilibili_qr_credentials.json"
QR_IMAGE_FILENAME = "bilibili_qr_login.png"
MAX_RESPONSE_BYTES = 512 * 1024
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class BilibiliQrLoginError(RuntimeError):
    """A QR-login failure that is safe to show to a plugin administrator."""


@dataclass(frozen=True)
class QrLoginOutcome:
    status: str
    message: str


@dataclass(frozen=True)
class QrLoginStart:
    qr_image_path: Path
    reused_existing_qr: bool
    task: asyncio.Task[QrLoginOutcome]


@dataclass
class _QrLoginChallenge:
    qr_url: str
    qrcode_key: str


@dataclass
class _ActiveQrLogin:
    challenge: _QrLoginChallenge
    session: aiohttp.ClientSession
    cancel_event: asyncio.Event
    task: asyncio.Task[QrLoginOutcome] | None = None


class BilibiliCredentialStore:
    """Persist only QR-login cookies in the plugin data directory."""

    source_label = "扫码登录保存的凭据"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = self.data_dir / SAVED_CREDENTIALS_FILENAME

    def cookie_header(self) -> str:
        payload = self._load_payload()
        raw_cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(raw_cookies, dict):
            return ""
        pairs = self._normalize_cookie_pairs(raw_cookies)
        return self._format_cookie_header(pairs)

    def has_credentials(self) -> bool:
        return bool(self.cookie_header())

    async def save_cookie_pairs(self, pairs: dict[str, str]) -> None:
        normalized = self._normalize_cookie_pairs(pairs)
        if not clean_text(normalized.get("SESSDATA")):
            raise BilibiliQrLoginError("登录成功但没有取得 SESSDATA，未保存凭据。")
        await asyncio.to_thread(self._save_sync, normalized)

    async def clear(self) -> bool:
        return await asyncio.to_thread(self._clear_sync)

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "[HelperTools/Bilibili] could not read saved QR credentials: %r",
                exc,
            )
            return {}
        return value if isinstance(value, dict) else {}

    def _save_sync(self, pairs: dict[str, str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": int(time.time()),
            "cookies": pairs,
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self._restrict_file_permissions(temporary)
            os.replace(temporary, self.path)
            self._restrict_file_permissions(self.path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _clear_sync(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "[HelperTools/Bilibili] could not clear saved QR credentials: %r",
                exc,
            )
            return False
        return True

    @staticmethod
    def _normalize_cookie_pairs(values: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_name, raw_value in values.items():
            name = clean_text(raw_name)
            value = clean_text(raw_value)
            if (
                not _COOKIE_NAME_RE.fullmatch(name)
                or not value
                or any(character in value for character in (";", "\r", "\n"))
            ):
                continue
            normalized[name] = value
        return normalized

    @staticmethod
    def _format_cookie_header(pairs: dict[str, str]) -> str:
        return "; ".join(f"{name}={value}" for name, value in pairs.items())

    @staticmethod
    def _restrict_file_permissions(path: Path) -> None:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


class BilibiliQrLoginService:
    """Generate, poll, and persist an administrator-initiated Bilibili QR login."""

    def __init__(
        self,
        config: Any,
        data_dir: Path,
        credentials: BilibiliCredentialStore,
    ) -> None:
        self.config = config
        self.data_dir = data_dir
        self.credentials = credentials
        self.qr_image_path = self.data_dir / QR_IMAGE_FILENAME
        self._lock = asyncio.Lock()
        self._active: _ActiveQrLogin | None = None

    def enabled(self) -> bool:
        return read_bool(self._config_value("enabled", True), True)

    def commands_enabled(self) -> bool:
        return self.enabled() and read_bool(
            self._config_value("commands_enabled", True),
            True,
        )

    def private_chat_only(self) -> bool:
        return read_bool(self._config_value("private_chat_only", True), True)

    def poll_interval_seconds(self) -> int:
        return read_int(
            self._config_value("poll_interval_seconds", 2),
            2,
            minimum=1,
            maximum=15,
        )

    def timeout_seconds(self) -> int:
        return read_int(
            self._config_value("timeout_seconds", 180),
            180,
            minimum=30,
            maximum=600,
        )

    def is_active(self) -> bool:
        active = self._active
        return active is not None and active.task is not None and not active.task.done()

    async def start_login(self) -> QrLoginStart:
        if not self.enabled():
            raise BilibiliQrLoginError("B 站扫码登录功能当前未启用。")

        async with self._lock:
            active = self._active
            if (
                active is not None
                and active.task is not None
                and not active.task.done()
                and not active.cancel_event.is_set()
            ):
                return QrLoginStart(
                    qr_image_path=self.qr_image_path,
                    reused_existing_qr=True,
                    task=active.task,
                )

            session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                trust_env=True,
            )
            try:
                challenge = await self._generate_challenge(session)
                await asyncio.to_thread(
                    self._write_qr_image,
                    challenge.qr_url,
                    self.qr_image_path,
                )
            except asyncio.CancelledError:
                await session.close()
                raise
            except Exception:
                await session.close()
                raise

            active = _ActiveQrLogin(
                challenge=challenge,
                session=session,
                cancel_event=asyncio.Event(),
            )
            active.task = asyncio.create_task(
                self._complete_login(active),
                name="helper-tools-bilibili-qr-login",
            )
            self._active = active
            return QrLoginStart(
                qr_image_path=self.qr_image_path,
                reused_existing_qr=False,
                task=active.task,
            )

    async def wait_for_login(self, start: QrLoginStart) -> QrLoginOutcome:
        return await asyncio.shield(start.task)

    async def cancel_login(self) -> bool:
        async with self._lock:
            active = self._active
            if active is None or active.task is None or active.task.done():
                return False
            active.cancel_event.set()
            return True

    async def cancel_login_and_wait(self) -> bool:
        async with self._lock:
            active = self._active
            if active is None or active.task is None or active.task.done():
                return False
            active.cancel_event.set()
            task = active.task
        await asyncio.shield(task)
        return True

    async def close(self) -> None:
        async with self._lock:
            active = self._active
            self._active = None
        if active is not None:
            if active.task is not None and not active.task.done():
                active.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await active.task
            if not active.session.closed:
                await active.session.close()
        await asyncio.to_thread(self._remove_qr_image)

    async def clear_qr_image(self) -> None:
        await asyncio.to_thread(self._remove_qr_image)

    def _qr_config(self) -> dict[str, Any]:
        value = cfg(self.config, "bilibili_video", "qr_login", {})
        return value if isinstance(value, dict) else {}

    def _config_value(self, key: str, default: Any) -> Any:
        return self._qr_config().get(key, default)

    def _request_timeout(self) -> int:
        return read_int(
            cfg(self.config, "bilibili_video", "request_timeout_seconds", 20),
            20,
            minimum=5,
            maximum=120,
        )

    async def _generate_challenge(
        self,
        session: aiohttp.ClientSession,
    ) -> _QrLoginChallenge:
        async with session.get(
            QR_GENERATE_ENDPOINT,
            headers=self._request_headers(),
            timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
        ) as response:
            payload = await self._read_json_response(response)
        self._ensure_success_payload(payload, "获取二维码")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliQrLoginError("B 站没有返回二维码数据，请稍后重试。")
        qr_url = clean_text(data.get("url"))
        qrcode_key = clean_text(data.get("qrcode_key"))
        if not qr_url or not qrcode_key:
            raise BilibiliQrLoginError("B 站返回的二维码数据不完整，请稍后重试。")
        if not self._is_bilibili_qr_url(qr_url):
            raise BilibiliQrLoginError("B 站返回了不受信任的二维码地址，已拒绝使用。")
        return _QrLoginChallenge(qr_url=qr_url, qrcode_key=qrcode_key)

    async def _complete_login(self, active: _ActiveQrLogin) -> QrLoginOutcome:
        deadline = time.monotonic() + self.timeout_seconds()
        try:
            while time.monotonic() < deadline:
                if active.cancel_event.is_set():
                    return QrLoginOutcome("cancelled", "扫码登录已取消。")
                outcome = await self._poll_login(active)
                if outcome is not None:
                    return outcome
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        active.cancel_event.wait(),
                        timeout=min(self.poll_interval_seconds(), remaining),
                    )
                except asyncio.TimeoutError:
                    continue
            return QrLoginOutcome("timeout", "等待扫码超时，请重新执行登录命令。")
        except asyncio.CancelledError:
            raise
        except BilibiliQrLoginError as exc:
            logger.warning("[HelperTools/Bilibili] QR login failed: %s", exc)
            return QrLoginOutcome("failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - keep background login failures contained
            logger.warning("[HelperTools/Bilibili] QR login failed unexpectedly: %r", exc)
            return QrLoginOutcome("failed", "扫码登录请求失败，请稍后重试。")
        finally:
            if not active.session.closed:
                await active.session.close()
            async with self._lock:
                if self._active is active:
                    self._active = None

    async def _poll_login(
        self,
        active: _ActiveQrLogin,
    ) -> QrLoginOutcome | None:
        async with active.session.get(
            QR_POLL_ENDPOINT,
            params={"qrcode_key": active.challenge.qrcode_key},
            headers=self._request_headers(),
            timeout=aiohttp.ClientTimeout(total=self._request_timeout()),
        ) as response:
            payload = await self._read_json_response(response)
            self._ensure_success_payload(payload, "检查扫码状态")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise BilibiliQrLoginError("B 站没有返回扫码状态，请稍后重试。")
            state_code = self._safe_int(data.get("code"))
            message = clean_text(data.get("message")) or clean_text(payload.get("message"))
            if state_code == 0:
                if active.cancel_event.is_set():
                    return QrLoginOutcome("cancelled", "扫码登录已取消。")
                cookies = self._collect_login_cookies(active.session, response)
                await self.credentials.save_cookie_pairs(cookies)
                return QrLoginOutcome("success", "扫码登录成功，凭据已安全保存。")
            if state_code in {86101, 86090}:
                return None
            if state_code == 86038:
                return QrLoginOutcome("expired", "登录二维码已过期，请重新执行登录命令。")
            detail = message or f"B 站返回状态码 {state_code}。"
            return QrLoginOutcome("failed", f"扫码登录失败：{detail}")

    @staticmethod
    def _request_headers() -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    @staticmethod
    async def _read_json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
        body = await response.content.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise BilibiliQrLoginError("B 站二维码接口返回内容过大，已中止登录。")
        if response.status < 200 or response.status >= 300:
            raise BilibiliQrLoginError(
                f"B 站二维码接口返回 HTTP {response.status}，请稍后重试。"
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliQrLoginError("B 站二维码接口返回了无法识别的数据。") from exc
        if not isinstance(payload, dict):
            raise BilibiliQrLoginError("B 站二维码接口返回格式不正确。")
        return payload

    @staticmethod
    def _ensure_success_payload(payload: dict[str, Any], action: str) -> None:
        if BilibiliQrLoginService._safe_int(payload.get("code")) == 0:
            return
        message = clean_text(payload.get("message")) or clean_text(payload.get("msg"))
        suffix = f"：{message}" if message else "。"
        raise BilibiliQrLoginError(f"{action}失败{suffix}")

    @staticmethod
    def _collect_login_cookies(
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
    ) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for cookie_source in (
            session.cookie_jar.filter_cookies(URL(BILIBILI_COOKIE_URL)),
            response.cookies,
        ):
            for name, morsel in cookie_source.items():
                value = clean_text(getattr(morsel, "value", morsel))
                if _COOKIE_NAME_RE.fullmatch(name) and value:
                    pairs[name] = value
        return pairs

    @staticmethod
    def _is_bilibili_qr_url(value: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(value)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        return parsed.scheme == "https" and (
            host == "bilibili.com" or host.endswith(".bilibili.com")
        )

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _write_qr_image(qr_url: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=4,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white")
            image.save(temporary, format="PNG")
            BilibiliCredentialStore._restrict_file_permissions(temporary)
            os.replace(temporary, path)
            BilibiliCredentialStore._restrict_file_permissions(path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _remove_qr_image(self) -> None:
        with contextlib.suppress(OSError):
            self.qr_image_path.unlink(missing_ok=True)
