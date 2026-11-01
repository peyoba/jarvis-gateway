# -*- coding: utf-8 -*-
"""开发板网关地址（hatch.host）的自动同步与守护。

ESP32 端把网关地址存在 NVS 的 hatch.host 里；Mac 的 Wi-Fi 地址一变（DHCP 续租、
换网络、切回有线），板子就再也连不上网关，表现为「按了没反应」。

这里用 USB 串口把 setup 命令 >hatch.host=<IP> 发进板子的控制台，看到回显
cmd hatch.host=... -> ok 即视为写入成功。串口只在本机地址和「板子上记录的地址」
不一致时才会打开——打开串口会让板子复位一次，因此不做无意义的重置。

对外能力：
  * lan_ip()            取默认路由出口的本机 IPv4 地址
  * push_host()         一次性把地址写进板子
  * needs_push()        判断板子上的地址是否需要更新
  * python -m jarvis_gateway.ip_sync guard   常驻守护，地址一变就写回板子
"""

import argparse
import glob
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

PORT_PATTERNS = (
    "/dev/cu.usbserial-*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.usbmodem*",
    "/dev/cu.SLAB*",
)
BAUD = 115200
STATE_NAME = "gateway_ip.txt"
LOCK_NAME = "ip_guard.lock"
DEFAULT_INTERVAL = 10

ROOT = Path(__file__).resolve().parents[2]


def lan_ip() -> str:
    """取本机在局域网里的 IPv4 地址。

    优先问 macOS 的 ipconfig（en0 通常是 Wi-Fi，en1 是有线/雷雳网卡），
    失败时退化成「对默认网关做一次 UDP connect 看出口地址」，不依赖网卡名字。
    """
    for iface in ("en0", "en1"):
        try:
            out = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        if out:
            return out
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.0.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def find_serial_port() -> Optional[str]:
    """找开发板的 USB 串口；没插板子时返回 None。"""
    for pattern in PORT_PATTERNS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def _open_serial(port: str):
    """打开串口，并尽量不让 DTR/RTS 把板子拉回复位。"""
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError("pyserial 未安装（python3 -m pip install pyserial）") from exc
    try:
        return serial.Serial(port, BAUD, timeout=0.2, dtr=False, rts=False)
    except Exception:
        # 老版本 pyserial 不接受 dtr/rts 关键字，退回手动设置
        handle = serial.Serial(port, BAUD, timeout=0.2)
        for attr in ("dtr", "rts"):
            try:
                setattr(handle, attr, False)
            except Exception:
                pass
        return handle


def push_host(ip: str, port: Optional[str] = None, wait: float = 4.0) -> Tuple[bool, str, Optional[str]]:
    """把网关地址写进开发板，返回 (是否成功, 说明, 串口)。"""
    target = port or find_serial_port()
    if not target:
        return False, "未找到串口：开发板没有插在这台 Mac 上", None
    try:
        handle = _open_serial(target)
    except RuntimeError as exc:
        return False, str(exc), target
    except Exception as exc:
        return False, "打开串口失败: %s" % exc, target

    buf = b""
    try:
        time.sleep(0.3)
        try:
            handle.reset_input_buffer()
        except Exception:
            pass
        handle.write((">hatch.host=%s\n" % ip).encode())
        handle.flush()
        deadline = time.time() + wait
        while time.time() < deadline:
            chunk = handle.read(4096)
            if not chunk:
                continue
            buf += chunk
            if b"cmd hatch.host" in buf and b"-> ok" in buf:
                break
    finally:
        try:
            handle.close()
        except Exception:
            pass

    text = buf.decode("utf-8", "replace")
    ok = "cmd hatch.host" in text and "-> ok" in text
    tail = [line for line in text.splitlines() if "hatch.host" in line]
    detail = "；".join(tail) or text[-300:].strip() or "无回显"
    return ok, detail, target


# --------------------------------------------------------------------------- #
# 同步状态（板子上一次被写入的地址）
# --------------------------------------------------------------------------- #

def state_path(run_dir: Optional[Path] = None) -> Path:
    base = Path(run_dir) if run_dir else ROOT / "run"
    return base / STATE_NAME


def read_applied(run_dir: Optional[Path] = None) -> str:
    try:
        return state_path(run_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_applied(ip: str, run_dir: Optional[Path] = None) -> None:
    path = state_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(ip + "\n", encoding="utf-8")


def needs_push(ip: str, run_dir: Optional[Path] = None) -> bool:
    return bool(ip) and ip != read_applied(run_dir)


# --------------------------------------------------------------------------- #
# 守护模式
# --------------------------------------------------------------------------- #

def _log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def _notify(title: str, text: str) -> None:
    if sys.platform != "darwin":
        return
    script = "display notification %s with title %s" % (
        json.dumps(text, ensure_ascii=False), json.dumps(title, ensure_ascii=False))
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _acquire_lock(run_dir: Path):
    """文件锁保证只有一个守护在跑，避免重复下发和重复通知。"""
    import fcntl

    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (run_dir / LOCK_NAME).open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("已有 IP 守护在运行，本次退出")
        sys.exit(0)
    return handle


def guard(interval: int = DEFAULT_INTERVAL, run_dir: Optional[Path] = None,
          notify: bool = True) -> int:
    base = Path(run_dir) if run_dir else ROOT / "run"
    lock = _acquire_lock(base)
    _log("IP 守护启动：每 %ds 检查一次（当前 %s）" % (interval, lan_ip() or "无网络"))
    last_fail_log = 0.0
    try:
        while True:
            ip = lan_ip()
            if needs_push(ip, base):
                ok, detail, port = push_host(ip)
                if ok:
                    old = read_applied(base)
                    write_applied(ip, base)
                    _log("已把网关地址写进开发板：%s → %s (%s)" % (old or "(首次)", ip, port))
                    if notify:
                        _notify("Jarvis 网关地址已同步", "%s → %s" % (old or "首次", ip))
                else:
                    now = time.time()
                    if now - last_fail_log > 60:
                        _log("地址 %s 尚未写入开发板：%s（会持续重试）" % (ip, detail))
                        last_fail_log = now
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.close()


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "guard":
        parser = argparse.ArgumentParser(prog="jarvis ip-guard")
        parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
        args = parser.parse_args(argv[1:])
        return guard(max(2, args.interval))

    parser = argparse.ArgumentParser(prog="jarvis ip-sync", description="把网关地址写进开发板")
    parser.add_argument("--ip", default=None, help="要写入的地址，缺省取本机地址")
    parser.add_argument("--port", default=None, help="串口设备，缺省自动查找")
    parser.add_argument("--force", action="store_true", help="地址没变也重新写入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本调用")
    args = parser.parse_args(argv)

    ip = args.ip or lan_ip()
    if not ip:
        print("✗ 取不到本机 IP")
        return 1
    if not args.force and not needs_push(ip):
        ok, detail, port = True, "地址未变化，跳过写入（需要强制可加 --force）", None
    else:
        ok, detail, port = push_host(ip, args.port)
    if args.json:
        print(json.dumps({"ok": ok, "ip": ip, "port": port, "detail": detail}, ensure_ascii=False))
    elif ok:
        print("✓ 开发板网关地址：%s" % ip + ("" if port is None else "（串口 %s）" % port))
        print("  %s" % detail)
        if port is not None:
            print("  提示：打开串口会让开发板复位约 7 秒，属于正常现象")
    else:
        print("✗ 写入失败（目标 %s / 串口 %s）" % (ip, port))
        print("  %s" % detail)
    if ok and port is not None:
        write_applied(ip)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
