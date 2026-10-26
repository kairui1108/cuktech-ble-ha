"""打包一致性：版本号、依赖清单、镜像文件清单这三处"手抄的第二份真值"。

为什么值得单独成文件——这类漂移的共同特征是**测试全绿、发布/部署即坏**：

* 版本号写在三处（`ble_server/pyproject.toml`、`ha_integration/pyproject.toml`、
  `ha_integration/.../manifest.json`）。HA 只读 manifest，用户看到的版本号来自
  那里，而 CHANGELOG/tag 对着 pyproject —— 漏改一处就是"发版了但用户还是旧号"。
* `ble_server/requirements.txt` 是 `pyproject.toml` dependencies 的第二份副本。
  漏 `requests` / `pycryptodomex` 的表现不是启动失败，而是首次点"小米扫码登录"
  才 ImportError（低频路径，最容易漏）。
* `ble_server/docker/Dockerfile` 的 COPY 是模块清单的第三份副本。多设备改造新增
  `adapter_coordinator.py` / `log_utils.py` 时就漏了，而 `ha_server.py` 顶层 import
  它们 —— 镜像启动即 `ModuleNotFoundError`。**CI 跑的是源码树，这条路径它碰不到**，
  所以这一条只能靠"清点清单"来守。

这三条都不是业务逻辑，用一条静态核对就能永久钉住。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+ 读 pyproject")

BLE_DIR = Path(__file__).resolve().parents[1]        # ble_server/
REPO_ROOT = BLE_DIR.parent                           # 仓库根
HA_DIR = REPO_ROOT / "ha_integration"
MANIFEST = HA_DIR / "custom_components" / "cuktech_charger" / "manifest.json"


def _pyproject_version(path: Path) -> str:
    with path.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _dep_names(entries) -> set[str]:
    """依赖声明 → 规范化包名（丢版本约束、`-`/`_` 视为同一个字符）。"""
    names = set()
    for raw in entries:
        name = re.split(r"[<>=!~\[;\s]", str(raw).strip(), maxsplit=1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


class TestVersionSync:
    """三处版本号必须同值。"""

    def test_ble_server_and_ha_versions_match(self):
        ble = _pyproject_version(BLE_DIR / "pyproject.toml")
        ha = _pyproject_version(HA_DIR / "pyproject.toml")
        assert ble == ha, (
            f"ble_server {ble!r} 与 ha_integration {ha!r} 版本号不一致："
            "发版时两处必须一起改")

    def test_manifest_matches_pyproject(self):
        """HA 只读 manifest 的 version —— 这一处漏改，用户看到的还是旧版本。"""
        ha = _pyproject_version(HA_DIR / "pyproject.toml")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        assert manifest["version"] == ha, (
            f"manifest.json {manifest['version']!r} 与 pyproject {ha!r} 不一致："
            "HA 界面显示的是 manifest 那份")


class TestDependencyLists:
    """requirements.txt / check_env.sh 都必须覆盖 pyproject 声明的依赖。"""

    def test_requirements_covers_pyproject_dependencies(self):
        declared = _dep_names(tomllib.loads(
            (BLE_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"])
        lines = [l.split("#")[0] for l in
                 (BLE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()]
        present = _dep_names([l for l in lines if l.strip()])
        missing = sorted(declared - present)
        assert not missing, (
            f"requirements.txt 缺 {missing}：它是 pyproject 的手抄副本，"
            "漏包的表现是命令行装出来的环境在低频路径上 ImportError")

    def test_check_env_checks_every_dependency(self):
        """check_env.sh 是用户唯一的自检入口：漏检查的包 = 装漏了也不报警。"""
        # 包名 → 导入名（paho-mqtt / pyyaml / pycryptodomex 都不等于包名）
        import_names = {
            "aiohttp": "aiohttp",
            "paho-mqtt": "paho.mqtt.client",
            "bleak": "bleak",
            "cryptography": "cryptography",
            "pyyaml": "yaml",
            "requests": "requests",
            "pycryptodomex": "Cryptodome",
        }
        declared = _dep_names(tomllib.loads(
            (BLE_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["dependencies"])
        unknown = sorted(d for d in declared if d not in import_names)
        assert not unknown, (
            f"新增依赖 {unknown} 没有登记导入名——请先把它补进本测试的 import_names，"
            "再加进 check_env.sh 的检查循环")
        script = (BLE_DIR / "check_env.sh").read_text(encoding="utf-8")
        m = re.search(r"^\s*for pkg in (.+?);\s*do\s*$", script, re.M)
        assert m, "check_env.sh 里找不到 `for pkg in ...; do` 依赖检查循环"
        checked = {tok.strip().strip('"\'') for tok in m.group(1).split()}
        expected = {import_names[d] for d in declared}
        # 双向比对：漏检查（装漏不报警）与多检查（要求装已经不需要的包）都要红。
        assert checked == expected, (
            f"check_env.sh 检查 {sorted(checked)}，而 pyproject 声明的是 "
            f"{sorted(expected)}（导入名）。两边必须一致：漏检 = 装漏了不报警；"
            "多检 = 让用户为一个已经不需要的包折腾")


class TestDockerImageFiles:
    """镜像里必须带上 ha_server 需要的**全部**顶层模块。

    CI 与 pytest 跑的都是源码树，`from adapter_coordinator import ...` 永远不会
    失败；只有镜像（按 Dockerfile 的 COPY 清单拼出来的那份文件集合）会缺文件。

    这里把 COPY 的**源模式按实际文件展开**再比对，所以 `COPY *.py ./` 也是有效
    断言（通配符匹配到什么就去比什么）——早先的版本遇到 `*.py` 直接返回"全覆盖"，
    等于唯一的护栏把自己关掉了：`COPY ha*.py ./` 照样通过。
    """

    def _copied_py(self) -> set[str]:
        """把 Dockerfile 的 COPY 源模式按 ble_server/ 实际文件展开成文件名集合。"""
        text = (BLE_DIR / "docker" / "Dockerfile").read_text(encoding="utf-8")
        copied: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY "):
                continue
            # COPY <src...> <dest>：最后一个词是目标，中间可能带 --chown 之类的旗标
            parts = [t for t in stripped.split()[1:] if not t.startswith("--")]
            for pattern in parts[:-1]:
                if "*" in pattern or "?" in pattern:
                    copied |= {p.name for p in BLE_DIR.glob(pattern) if p.is_file()}
                else:
                    name = Path(pattern).name
                    if name.endswith(".py"):
                        copied.add(name)
        return copied

    def test_every_top_level_module_is_in_the_image(self):
        copied = self._copied_py()
        modules = {p.name for p in BLE_DIR.glob("*.py")}
        missing = sorted(modules - copied)
        assert not missing, (
            f"Dockerfile 的 COPY 清单缺 {missing}：这些模块在镜像里不存在，"
            "而 ha_server 顶层 import 它们 → 镜像启动即 ModuleNotFoundError")

    def test_dockerignore_does_not_exclude_top_level_modules(self):
        """`COPY *.py` 还有一个洞：.dockerignore 能把匹配到的文件再踢出去。"""
        ignore = (BLE_DIR / ".dockerignore").read_text(encoding="utf-8")
        patterns = [l.strip() for l in ignore.splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        hit = [p for p in patterns if p in ("*.py", "*.py*", ".", "*")]
        assert not hit, f".dockerignore 的 {hit} 会把这些顶层模块排除在镜像之外"

    def test_dockerfile_still_installs_the_package(self):
        """反向护栏：别为了少抄一份清单，把 pyproject 安装也一起删了。"""
        text = (BLE_DIR / "docker" / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY src/ src/" in text
        assert "pip install" in text
