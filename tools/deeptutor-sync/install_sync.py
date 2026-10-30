"""Copy this reviewed helper kit beside an existing DeepTutor 1.7.0 venv."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil

FILES = ["deeptutor_sync.py", "test_handoff.py", "manage_deeptutor.py", "README.md", "VERIFICATION.md", "install_sync.py", "requirements-sync.txt"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path("D:/DeepTutor"))
    args = parser.parse_args()
    app = args.app_root.resolve()
    source = Path(__file__).resolve().parent
    target = app / "sync-kit"
    if not (app / ".venv/Scripts/python.exe").exists():
        raise SystemExit("先安装 DeepTutor 的 .venv，参见 README 中的家庭电脑步骤。")
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        origin, destination = source / name, target / name
        if origin.resolve() == destination.resolve():
            continue
        if destination.exists() and destination.read_bytes() != origin.read_bytes():
            backup = destination.with_name(destination.name + ".before-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(destination, backup)
        shutil.copy2(origin, destination)
    launcher = app / "manage_deeptutor.py"
    desired = (source / "manage_deeptutor.py").read_bytes()
    if launcher.exists() and launcher.read_bytes() != desired:
        shutil.copy2(launcher, launcher.with_name("manage_deeptutor.before-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".py"))
    launcher.write_bytes(desired)
    for name, action in [("结束学习并同步.cmd", "finish"), ("继续学习.cmd", "resume")]:
        text = '@echo off\r\nchcp 65001 >nul\r\n"%~dp0.venv\\Scripts\\python.exe" -X utf8 "%~dp0sync-kit\\deeptutor_sync.py" --app-root "%~dp0." ' + action + '\r\nif errorlevel 1 echo 请保留窗口中的提示，不要强制覆盖同步。\r\npause\r\n'
        (app / name).write_text(text, encoding="utf-8", newline="")
    print("已安装两个入口：" + str(app / "结束学习并同步.cmd") + "、" + str(app / "继续学习.cmd"))
    print("下一步初始化本机配置。不要把 sync-local 目录加入 Git。")


if __name__ == "__main__":
    main()
