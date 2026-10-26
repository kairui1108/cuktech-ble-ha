"""Config flow for CUKTECH Charger integration."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_DEVICES,
    CONF_DEVICE_ID,
    CONF_NAME_PINNED,
    CONF_SERVER_URL,
    CONF_TOPIC_PREFIX,
    DEFAULT_SERVER_URL,
    DOMAIN,
    HTTP_TIMEOUT,
    PRIMARY_DEVICE_ID,
    TOPIC_PREFIX,
    entry_string,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "CUKTECH 10 GaN Charger Ultra"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Optional(CONF_SERVER_URL, default=DEFAULT_SERVER_URL): str,
        # 留空 = 缺省前缀 cuktech/charger。为什么把前缀放在**第一屏**而不是单独
        # 一屏：ESP32 用户（发现端点 404）只按一次提交就完成，与改造前的表单
        # 输入量一致；改过 NVS 前缀的人才需要填这一格。发现端点可用的服务端
        # 这里填什么都会被服务端上报的前缀覆盖（前缀即设备，见 §3.6）。
        vol.Optional(CONF_TOPIC_PREFIX, default=""): str,
    }
)


def generate_unique_id(server_url: str, topic_prefix: str = TOPIC_PREFIX) -> str:
    """unique_id = cuktech_{md5(server_url|topic_prefix)[:16]}（§4.2）。

    加上前缀维度：同一台 server 上的两台设备靠前缀区分，否则第二个 entry 会被
    `_abort_if_unique_id_configured` 当成"已配置"直接拒掉。

    仅影响**新建** entry：老 entry 的 unique_id 是 md5(server_url)，不重算、
    不迁移（重算等于换 entry，实体 unique_id 全变、历史与自动化断裂）。
    """
    digest = hashlib.md5(f"{server_url}|{topic_prefix}".encode()).hexdigest()[:16]
    return f"cuktech_{digest}"


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Validate the user input allows us to connect.

    Returns a dict with "title" on success, raises ValueError on failure.
    """
    server_url = data.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
    session = async_get_clientsession(hass)
    try:
        url = f"{server_url}/api/status"
        # async with 确保连接被释放，避免泄漏给 GC
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                raise ValueError(f"Server returned status {resp.status}")
            await resp.read()  # 读取 body，确保连接完全复用/释放
    except ValueError:
        raise
    except Exception as err:
        raise ValueError(f"Cannot connect to server: {err}") from err
    return {"title": data.get(CONF_NAME, "CUKTECH Charger")}


def _normalise_device(raw: Any) -> dict[str, Any] | None:
    """把 /api/devices 的一项收敛成 flow 内部结构；认不出 id 的条目直接丢掉。"""
    if not isinstance(raw, dict):
        return None
    device_id = raw.get("id")
    if not isinstance(device_id, str) or not device_id.strip():
        return None
    name = raw.get("name")
    mac = raw.get("mac")
    prefix = raw.get("topic_prefix")
    return {
        "id": device_id.strip(),
        "name": name.strip() if isinstance(name, str) and name.strip() else device_id,
        "mac": mac if isinstance(mac, str) else "",
        "connected": bool(raw.get("connected")),
        "topic_prefix": prefix if isinstance(prefix, str) else "",
    }


async def async_discover_devices(
    hass: HomeAssistant, server_url: str
) -> list[dict[str, Any]] | None:
    """GET /api/devices（§4.2 的设备发现）。

    返回 None = "对端没有设备发现能力"，调用方按老的单设备流程处理：
      * ESP32 固件没有这个端点 → 404；
      * 旧版 ble_server → 404；
      * 超时/连不上/响应不可解析 → 同样当作"发现不了"。
    发现失败**不是**配置失败：单设备流程在任何后端上都可用，所以这里永不抛错。
    """
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{server_url}{API_DEVICES}", timeout=HTTP_TIMEOUT
        ) as resp:
            if resp.status != 200:
                await resp.read()
                _LOGGER.debug(
                    "Device discovery unavailable at %s (HTTP %s); "
                    "falling back to the single-device flow",
                    server_url, resp.status,
                )
                return None
            data = await resp.json()
    except Exception as err:
        _LOGGER.debug(
            "Device discovery failed at %s (%s); falling back to the single-device flow",
            server_url, err,
        )
        return None

    raw_devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(raw_devices, list):
        return None
    devices = [d for d in (_normalise_device(r) for r in raw_devices) if d]
    return devices or None


def _device_label(device: dict[str, Any]) -> str:
    """设备选择项的可读标签：名称 · MAC · 连接状态 · 主题前缀。

    四样信息缺一不可——同名同型号的两台充电器只靠名称根本选不出来，而前缀
    正是这个 entry 之后要吃的命名空间，让用户在这里就能核对自己选的是哪台。
    """
    parts = [device["name"]]
    if device["mac"]:
        parts.append(device["mac"])
    parts.append("已连接" if device["connected"] else "未连接")
    prefix = device["topic_prefix"] or TOPIC_PREFIX
    parts.append(prefix)
    return " · ".join(parts)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CUKTECH Charger."""

    VERSION = 1

    # 步骤间传递的状态。类级默认值是为了 HA 重启后从某个中间步骤恢复时不会
    # AttributeError（此时实例属性还没被赋值）。
    _pending: dict[str, Any] = {}
    _devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第一步：服务端地址 + 可选前缀，然后尝试设备发现。

        三种结局（§4.2）：
          * 发现端点 404/超时（ESP32、旧 server）→ 单设备流程，直接建 entry；
          * 只发现 1 台 → 自动选中，取其 topic_prefix（单设备 server 也走这条）；
          * 发现多台 → 进设备选择步骤，一设备一 entry。
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except ValueError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during setup")
                errors["base"] = "unknown"
            else:
                server_url = user_input.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
                self._pending = dict(user_input)
                devices = await async_discover_devices(self.hass, server_url)
                if devices is not None and len(devices) > 1:
                    self._devices = devices
                    return await self.async_step_device()
                return await self._async_create_device_entry(
                    devices[0] if devices else None
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第二步（仅多设备时）：从发现到的设备里选一台。"""
        if user_input is not None:
            wanted = user_input.get(CONF_DEVICE_ID)
            device = self._find_device(wanted)
            if device is None:
                # 选择期间服务端配置变了（重启/换了设备表）：重新发现一次再决定，
                # 既不猜、也不静默把 entry 建到别的设备上。
                _LOGGER.warning(
                    "Selected device %s is no longer advertised; re-running discovery",
                    wanted,
                )
                self._devices = await async_discover_devices(
                    self.hass, self._pending.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
                ) or []
                device = self._find_device(wanted)
            if device is not None:
                return await self._async_create_device_entry(device)
            if not self._devices:
                # 连设备列表都拿不出来了（服务端掉线/降级成旧版）：回第一步重来，
                # 而不是给用户一张空的下拉框。
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_DATA_SCHEMA,
                    errors={"base": "cannot_connect"},
                )
            return self.async_show_form(
                step_id="device",
                data_schema=self._device_schema(),
                errors={"base": "device_not_found"},
            )

        if not self._devices:
            # 流程从中间被恢复（HA 重启）但发现结果已丢：回第一步重新发现，
            # 不展示一张空的下拉框。
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors={}
            )

        return self.async_show_form(
            step_id="device", data_schema=self._device_schema(), errors={}
        )

    def _find_device(self, device_id) -> dict[str, Any] | None:
        """从发现结果里找一台设备。"""
        return next((d for d in self._devices if d["id"] == device_id), None)

    def _device_schema(self) -> vol.Schema:
        """设备下拉框。value 是 device_id，label 带上 name/MAC/状态/前缀。"""
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=d["id"], label=_device_label(d)
                            )
                            for d in self._devices
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )

    async def _async_create_device_entry(self, device: dict | None) -> FlowResult:
        """按"选中的设备（可能是 None = 单设备/ESP32）"落 entry。"""
        server_url = self._pending.get(CONF_SERVER_URL, DEFAULT_SERVER_URL)
        typed_prefix = (self._pending.get(CONF_TOPIC_PREFIX) or "").strip()
        name = (self._pending.get(CONF_NAME) or "").strip() or DEFAULT_NAME

        if device is not None:
            # 服务端上报的前缀就是这个 entry 该吃的命名空间，优先于用户手填
            topic_prefix = device["topic_prefix"] or TOPIC_PREFIX
            device_id = device["id"]
            name = device["name"]
            if name == device_id:
                # 服务端没给这台配 `name`（`_normalise_device` 回填的是 id）：
                # 直接拿 id 当显示名，多台就会看到 "desk"/"bed" 或一串同名；
                # 用默认名兜底，并给非 primary 补上 id 以免两台长得一模一样。
                name = (DEFAULT_NAME if device_id == PRIMARY_DEVICE_ID
                        else f"{DEFAULT_NAME} ({device_id})")
        else:
            topic_prefix = typed_prefix or TOPIC_PREFIX
            device_id = None

        data: dict[str, Any] = {
            CONF_NAME: name,
            CONF_SERVER_URL: server_url,
            # 显式落盘（而不是留空靠运行时回退）：options flow 改前缀时才有基准值，
            # unique_id 也才有明确输入。老 entry 没有这个键，照样运行时回退。
            CONF_TOPIC_PREFIX: topic_prefix,
        }
        if device_id:
            data[CONF_DEVICE_ID] = device_id

        await self.async_set_unique_id(generate_unique_id(server_url, topic_prefix))
        self._abort_if_unique_id_configured()
        # 设备名取 entry title（§4.5）：HA 侧显示的就是"书桌充电器"而不是一串同名设备
        return self.async_create_entry(title=name, data=data)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle re-authentication when the server URL changes."""
        await self.async_set_unique_id(self._existing_unique_id(entry_data))
        return await self.async_step_reauth_confirm()

    def _existing_unique_id(self, entry_data: dict[str, Any]) -> str:
        """沿用 entry 已有的 unique_id（老 entry 不重算，§4.2）。"""
        entry_id = self.context.get("entry_id") if isinstance(self.context, dict) else None
        entry = (
            self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        )
        unique_id = getattr(entry, "unique_id", None)
        if isinstance(unique_id, str) and unique_id:
            return unique_id
        return generate_unique_id(
            entry_data.get(CONF_SERVER_URL, DEFAULT_SERVER_URL),
            entry_data.get(CONF_TOPIC_PREFIX) or TOPIC_PREFIX,
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle re-authentication confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except ValueError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                if entry:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **user_input}
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_SERVER_URL): str}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry=None) -> "CuktechOptionsFlow":
        """暴露 options flow（§4.5）。

        `config_entry` 允许缺省：HA 2024.1~2024.10 会把它传进来，2024.11+ 改成由
        框架把 entry 注入到 OptionsFlow 上。两种都支持，升级 HA 不会炸。
        """
        return CuktechOptionsFlow(config_entry)


class CuktechOptionsFlow(config_entries.OptionsFlow):
    """创建后修改 topic 前缀 / device_id。

    没有它，改前缀的唯一路径是"删了重加"，而重加会换 entry_id —— 实体
    unique_id 全部重建，历史与自动化断裂（§4.5）。改动写入 entry.data 后立刻
    重载 entry：订阅、发布、限额请求全部按新前缀重新装配。
    """

    def __init__(self, config_entry=None) -> None:
        """Initialize the options flow."""
        super().__init__()
        self._entry = config_entry

    @property
    def _config_entry(self):
        """本 flow 操作的 entry：优先用构造时传入的，其次用框架注入的。"""
        if self._entry is not None:
            return self._entry
        return self.config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the options step."""
        entry = self._config_entry

        if user_input is not None:
            topic_prefix = (user_input.get(CONF_TOPIC_PREFIX) or "").strip()
            device_id = (user_input.get(CONF_DEVICE_ID) or "").strip()
            server_url = (user_input.get(CONF_SERVER_URL) or "").strip()
            new_data = {
                **dict(entry.data),
                CONF_TOPIC_PREFIX: topic_prefix or TOPIC_PREFIX,
            }
            if server_url:
                # 空值 = 没改（表单里这是必填项，默认值就是现值）。**绝不**在留空时
                # 删键：那会把 entry 悄悄指向缺省地址，用户只会看到"集成连不上了"。
                new_data[CONF_SERVER_URL] = server_url
            if device_id:
                new_data[CONF_DEVICE_ID] = device_id
            else:
                # 留空 = 解除设备绑定（回到 ESP32/旧 server 的单设备寻址方式）
                new_data.pop(CONF_DEVICE_ID, None)

            updates: dict[str, Any] = {"data": new_data}
            new_name = (user_input.get(CONF_NAME) or "").strip()
            # 显示名单独走 title（§4.5）：改名字不该改动 data，也不该换 unique_id。
            # 同时记下"这是用户起的名"：运行时的设备名自动同步遇到它就停手，否则
            # 用户改完名字、下一次 reload 就被服务端的名字顶回去（安静地失效）。
            if new_name and new_name != getattr(entry, "title", None):
                updates["title"] = new_name
                new_data[CONF_NAME_PINNED] = True
            elif not new_name:
                # 清空名称 = 交回自动同步（下次 setup 会按服务端上报的名字对齐）
                new_data.pop(CONF_NAME_PINNED, None)
            self.hass.config_entries.async_update_entry(entry, **updates)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME,
                        default=(getattr(entry, "title", None) or DEFAULT_NAME),
                    ): str,
                    vol.Required(
                        CONF_SERVER_URL,
                        default=entry_string(entry, CONF_SERVER_URL) or DEFAULT_SERVER_URL,
                    ): str,
                    vol.Required(
                        CONF_TOPIC_PREFIX,
                        default=entry_string(entry, CONF_TOPIC_PREFIX) or TOPIC_PREFIX,
                    ): str,
                    vol.Optional(
                        CONF_DEVICE_ID,
                        default=entry_string(entry, CONF_DEVICE_ID) or "",
                    ): str,
                }
            ),
            errors={},
        )
