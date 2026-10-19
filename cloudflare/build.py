"""只构建入口、共享纯解析器及网页，防止上传本地配置和参考目录。"""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / ".build"
if OUTPUT.is_symlink():
    raise RuntimeError("build output cannot be a symlink")
OUTPUT.mkdir(exist_ok=True)
(OUTPUT / "public").mkdir(exist_ok=True)
shutil.copyfile(ROOT / "worker.py", OUTPUT / "entry.py")
shutil.copyfile(ROOT.parent / "douyin_parser.py", OUTPUT / "douyin_parser.py")
shutil.copyfile(ROOT.parent / "original_metadata.py", OUTPUT / "original_metadata.py")
shutil.copyfile(ROOT.parent / "index.html", OUTPUT / "public/index.html")
print("Built the Worker entry, shared media parsers and public index only")
