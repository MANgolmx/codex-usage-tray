from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw, ImageFont


APP_NAME = "Codex Usage Tray"
APP_VERSION = "1.0.1"
REFRESH_SECONDS = 180
USAGE_URL = "https://chatgpt.com/codex/settings/usage"
NOTIFICATION_TITLE = "Codex usage info"


@dataclass
class LimitWindow:
    name: str
    remaining_percent: int
    used_percent: float
    resets_at: int | None
    duration_minutes: int | None

    @property
    def reset_text(self) -> str:
        if not self.resets_at:
            return "reset time unknown"
        dt = datetime.fromtimestamp(self.resets_at).astimezone()
        now = datetime.now().astimezone()
        if dt.date() == now.date():
            return f"resets at {dt:%H:%M}"
        return f"resets at {dt:%Y-%m-%d %H:%M}"

    @property
    def menu_text(self) -> str:
        return f"{self.name}: {self.remaining_percent}% · {self.reset_text}"


@dataclass
class UsageState:
    windows: list[LimitWindow]
    plan_type: str | None = None
    credit_balance: str | None = None
    unlimited_credits: bool = False
    updated_at: float = 0.0

    @property
    def lowest_remaining(self) -> int | None:
        if not self.windows:
            return None
        return min(w.remaining_percent for w in self.windows)

    @property
    def five_hour_remaining(self) -> int | None:
        """Return the short, five-hour limit for the tray badge when available."""
        for window in self.windows:
            if window.duration_minutes == 300:
                return window.remaining_percent
        return self.lowest_remaining

    @property
    def tooltip(self) -> str:
        if not self.windows:
            return "Codex Usage: no data"
        parts = [f"{w.name} {w.remaining_percent}%" for w in self.windows[:2]]
        return "Codex: " + " | ".join(parts)


class CodexAppServerError(RuntimeError):
    pass


class CodexAppServerClient:
    """Small JSON-RPC client for `codex app-server --stdio`."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.reader_thread: threading.Thread | None = None
        self.next_id = 1
        self.request_lock = threading.Lock()

    def _build_command(self) -> list[str]:
        codex = shutil.which("codex")
        if not codex:
            raise CodexAppServerError(
                "Codex CLI was not found in PATH. Run `codex --version` to check it."
            )

        suffix = Path(codex).suffix.lower()
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            command_line = subprocess.list2cmdline([codex, "app-server", "--stdio"])
            return [comspec, "/d", "/s", "/c", command_line]

        return [codex, "app-server", "--stdio"]

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        self.stop()
        self.messages = queue.Queue()

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.process = subprocess.Popen(
            self._build_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        if not self.process.stdin or not self.process.stdout:
            self.stop()
            raise CodexAppServerError("Could not open Codex app-server stdio.")

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name="codex-app-server-reader",
            daemon=True,
        )
        self.reader_thread.start()

        init_result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_usage_tray",
                    "title": APP_NAME,
                    "version": APP_VERSION,
                }
            },
            timeout=15,
        )
        if not isinstance(init_result, dict):
            self.stop()
            raise CodexAppServerError("Codex app-server returned an invalid initialize response.")

        self.notify("initialized")

    def _reader_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self.messages.put(message)
        finally:
            self.messages.put({"__eof__": True})

    def _send(self, message: dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise CodexAppServerError("Codex app-server is not running.")

        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("Connection to Codex app-server was lost.") from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 12,
    ) -> Any:
        with self.request_lock:
            request_id = self.next_id
            self.next_id += 1

            message: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._send(message)

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexAppServerError(f"Timed out waiting for {method}.")

                try:
                    response = self.messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise CodexAppServerError(f"Timed out waiting for {method}.") from exc

                if response.get("__eof__"):
                    raise CodexAppServerError("Codex app-server exited.")

                # Notifications are expected and can arrive between request/response pairs.
                if response.get("id") != request_id:
                    continue

                if "error" in response:
                    error = response["error"]
                    if isinstance(error, dict):
                        text = error.get("message") or json.dumps(error, ensure_ascii=False)
                    else:
                        text = str(error)
                    raise CodexAppServerError(f"{method}: {text}")

                return response.get("result")

    def get_rate_limits(self) -> dict[str, Any]:
        self.start()
        result = self.request("account/rateLimits/read", timeout=15)
        if not isinstance(result, dict):
            raise CodexAppServerError("account/rateLimits/read returned invalid data.")
        return result

    def stop(self) -> None:
        process = self.process
        self.process = None
        if not process:
            return

        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def _window_name(minutes: int | None, fallback: str) -> str:
    if minutes == 300:
        return "5 hours"
    if minutes == 10080:
        return "Week"
    if minutes and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} days"
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hours"
    if minutes:
        return f"{minutes} minutes"
    return fallback


def _parse_window(raw: Any, fallback: str) -> LimitWindow | None:
    if not isinstance(raw, dict):
        return None

    used = raw.get("usedPercent")
    if used is None:
        return None

    try:
        used_value = float(used)
    except (TypeError, ValueError):
        return None

    remaining = round(100 - used_value)
    remaining = max(0, min(100, remaining))

    duration = raw.get("windowDurationMins")
    try:
        duration_int = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_int = None

    resets = raw.get("resetsAt")
    try:
        resets_int = int(resets) if resets is not None else None
    except (TypeError, ValueError):
        resets_int = None

    return LimitWindow(
        name=_window_name(duration_int, fallback),
        remaining_percent=remaining,
        used_percent=used_value,
        resets_at=resets_int,
        duration_minutes=duration_int,
    )


def parse_usage(raw: dict[str, Any]) -> UsageState:
    snapshot = raw.get("rateLimits")
    by_id = raw.get("rateLimitsByLimitId")

    # Prefer the explicit "codex" bucket when a multi-bucket response is available.
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        snapshot = by_id["codex"]

    if not isinstance(snapshot, dict):
        snapshot = {}

    windows: list[LimitWindow] = []
    primary = _parse_window(snapshot.get("primary"), "Primary")
    secondary = _parse_window(snapshot.get("secondary"), "Secondary")
    if primary:
        windows.append(primary)
    if secondary:
        windows.append(secondary)

    credits = snapshot.get("credits")
    credit_balance = None
    unlimited_credits = False
    if isinstance(credits, dict):
        unlimited_credits = bool(credits.get("unlimited"))
        balance = credits.get("balance")
        if balance is not None:
            credit_balance = str(balance)

    plan_type = snapshot.get("planType")
    if plan_type is not None:
        plan_type = str(plan_type)

    return UsageState(
        windows=windows,
        plan_type=plan_type,
        credit_balance=credit_balance,
        unlimited_credits=unlimited_credits,
        updated_at=time.time(),
    )


def make_icon(text: str, error: bool = False) -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    fill = (165, 45, 45, 255) if error else (45, 45, 48, 255)
    draw.ellipse((2, 2, size - 2, size - 2), fill=fill)

    try:
        # The Windows notification area downsizes this 64 px image substantially.
        # Large numerals preserve a legible 5-hour percentage at tray-icon size.
        font_size = 42 if len(text) <= 2 else 32
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    x = (size - (bbox[2] - bbox[0])) / 2
    y = (size - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)
    return image


class TrayApp:
    def __init__(self) -> None:
        self.client = CodexAppServerClient()
        self.state: UsageState | None = None
        self.last_error: str | None = None
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.has_notified_error = False

        self.icon = pystray.Icon(
            "codex_usage_tray",
            make_icon("--"),
            "Codex Usage: loading...",
        )
        self.icon.menu = self._build_menu()

    def _noop(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        pass

    def _build_menu(self) -> pystray.Menu:
        items: list[Any] = []

        if self.state and self.state.windows:
            for window in self.state.windows:
                items.append(pystray.MenuItem(window.menu_text, self._noop, enabled=False))

            if self.state.credit_balance is not None or self.state.unlimited_credits:
                if self.state.unlimited_credits:
                    credit_text = "Credits: unlimited"
                else:
                    credit_text = f"Credits: {self.state.credit_balance}"
                items.append(pystray.MenuItem(credit_text, self._noop, enabled=False))

            updated = datetime.fromtimestamp(self.state.updated_at).astimezone()
            items.append(
                pystray.MenuItem(
                    f"Updated: {updated:%H:%M:%S}",
                    self._noop,
                    enabled=False,
                )
            )
        elif self.last_error:
            short_error = self.last_error.replace("\n", " ")
            if len(short_error) > 90:
                short_error = short_error[:87] + "..."
            items.append(pystray.MenuItem(short_error, self._noop, enabled=False))
        else:
            items.append(pystray.MenuItem("Loading...", self._noop, enabled=False))

        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Show limits", self._show_status, default=True),
                pystray.MenuItem("Refresh", self._request_refresh),
                pystray.MenuItem("Open Usage", self._open_usage),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self._quit),
            ]
        )
        return pystray.Menu(*items)

    def _show_status(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self.state and self.state.windows:
            lines = [w.menu_text for w in self.state.windows]
            if self.state.credit_balance is not None:
                lines.append(f"Credits: {self.state.credit_balance}")
            icon.notify("\n".join(lines), NOTIFICATION_TITLE)
        elif self.last_error:
            icon.notify(self.last_error, NOTIFICATION_TITLE)
        else:
            icon.notify("Data is still loading.", NOTIFICATION_TITLE)

    def _request_refresh(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.refresh_event.set()

    def _open_usage(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        webbrowser.open(USAGE_URL)

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.stop_event.set()
        self.refresh_event.set()
        self.client.stop()
        icon.stop()

    def _set_error(self, message: str) -> None:
        self.last_error = message
        self.state = None
        self.icon.icon = make_icon("!", error=True)
        self.icon.title = "Codex Usage: error"
        self.icon.menu = self._build_menu()
        self.icon.update_menu()

        if not self.has_notified_error:
            self.has_notified_error = True
            self.icon.notify(message, NOTIFICATION_TITLE)

    def _set_state(self, state: UsageState) -> None:
        self.state = state
        self.last_error = None
        self.has_notified_error = False

        five_hour_remaining = state.five_hour_remaining
        icon_text = "--" if five_hour_remaining is None else str(five_hour_remaining)
        self.icon.icon = make_icon(icon_text)
        self.icon.title = state.tooltip
        self.icon.menu = self._build_menu()
        self.icon.update_menu()

    def _fetch_once(self) -> None:
        try:
            raw = self.client.get_rate_limits()
            state = parse_usage(raw)
            if not state.windows:
                raise CodexAppServerError(
                    "Codex responded, but did not return any active rate-limit windows."
                )
            self._set_state(state)
            return
        except Exception as first_error:
            # Restart once. This handles Codex updates, stale local app-server processes,
            # and broken stdio pipes without requiring a tray restart.
            self.client.stop()
            try:
                raw = self.client.get_rate_limits()
                state = parse_usage(raw)
                if not state.windows:
                    raise CodexAppServerError(
                        "Codex responded, but did not return any active rate-limit windows."
                    )
                self._set_state(state)
                return
            except Exception as second_error:
                # Leave every failed attempt with a clean client. The next automatic
                # or manual refresh will then start a brand-new app-server process.
                self.client.stop()
                self._set_error(str(second_error or first_error))

    def _refresh_safely(self) -> None:
        """Keep the background updater alive even if a UI update fails."""
        try:
            self._fetch_once()
        except Exception as exc:
            message = f"Unexpected refresh error: {exc}"
            self.last_error = message
            self.state = None
            self.client.stop()
            try:
                self._set_error(message)
            except Exception:
                # A tray backend error must not terminate the retry loop.
                pass

    def _update_loop(self) -> None:
        while not self.stop_event.is_set():
            self._refresh_safely()
            if self.stop_event.is_set():
                break
            self.refresh_event.wait(REFRESH_SECONDS)
            self.refresh_event.clear()

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(
            target=self._update_loop,
            name="codex-usage-updater",
            daemon=True,
        ).start()

    def run(self) -> None:
        try:
            self.icon.run(setup=self._setup)
        finally:
            self.stop_event.set()
            self.refresh_event.set()
            self.client.stop()


if __name__ == "__main__":
    TrayApp().run()
