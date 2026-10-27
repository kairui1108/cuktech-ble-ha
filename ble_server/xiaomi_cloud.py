"""Xiaomi Cloud API — lightweight wrapper for token/ble_key extraction.

Based on PiotrMachowski/Xiaomi-cloud-tokens-extractor (MIT License).
Only the password login + device listing + beaconkey methods are retained.
"""
import base64
import hashlib
import json
import logging
import os
import random
import string
import time
from typing import Optional

import requests
try:
    from Crypto.Cipher import ARC4
except ModuleNotFoundError:
    from Cryptodome.Cipher import ARC4

_LOGGER = logging.getLogger("xiaomi_cloud")

SERVERS = {"cn": "https://api.io.mi.com/app",
           "de": "https://de.api.io.mi.com/app",
           "us": "https://us.api.io.mi.com/app",
           "ru": "https://ru.api.io.mi.com/app",
           "tw": "https://tw.api.io.mi.com/app",
           "sg": "https://sg.api.io.mi.com/app",
           "in": "https://in.api.io.mi.com/app",
           "i2": "https://i2.api.io.mi.com/app"}


def _generate_agent():
    agent = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-" + \
            "".join(random.choices(string.ascii_uppercase + string.digits, k=11))
    return agent + " MIIO/" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _generate_device_id():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def _generate_nonce(millis):
    nonce_bytes = os.urandom(8) + (int(millis / 60000)).to_bytes(4, byteorder="big")
    return base64.b64encode(nonce_bytes).decode()


def _signed_nonce(ssecurity, nonce):
    hash_obj = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce))
    return base64.b64encode(hash_obj.digest()).decode()


def _encrypt_rc4(password, payload):
    """RC4-drop[1024] encrypt, matching original token_extractor."""
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))
    return base64.b64encode(r.encrypt(payload.encode())).decode()


def _decrypt_rc4(password, payload):
    """RC4-drop[1024] decrypt, matching original token_extractor."""
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))
    return r.encrypt(base64.b64decode(payload))


def _generate_enc_signature(url, method, signed_nonce, params):
    """Generate encrypted signature, matching original token_extractor."""
    signature_params = [str(method).upper(), url.split("com")[1].replace("/app/", "/")]
    for k, v in params.items():
        signature_params.append(f"{k}={v}")
    signature_params.append(signed_nonce)
    signature_string = "&".join(signature_params)
    return base64.b64encode(hashlib.sha1(signature_string.encode("utf-8")).digest()).decode()


def _generate_enc_params(url, method, signed_nonce, nonce, params, ssecurity):
    """Generate encrypted API params, matching original token_extractor."""
    params["rc4_hash__"] = _generate_enc_signature(url, method, signed_nonce, params)
    for k, v in params.items():
        params[k] = _encrypt_rc4(signed_nonce, v)
    params.update({
        "signature": _generate_enc_signature(url, method, signed_nonce, params),
        "ssecurity": ssecurity,
        "_nonce": nonce,
    })
    return params


class XiaomiCloudLoginError(Exception):
    pass


#: 云 HTTP 超时（连接, 读取）。**必须显式给**：`requests` 默认无限等，而调用方是
#: `run_in_executor` 里的阻塞线程——一旦云端卡住，线程和 `requests.Session` 就永久
#: 挂在那里（executor 没有取消路径），30s 的请求超时只把浏览器救了，服务端照样漏。
#: 批量导入把"一次悬死"放大成 N 次，所以这里是硬性要求而不是优化。
#: 读超时 15s 与 API 请求的 30s 上限留出一半余量，够一次云调用往返。
CLOUD_HTTP_TIMEOUT = (5, 15)


class XiaomiCloudDevice:
    """Represents a device from Xiaomi cloud."""
    def __init__(self, did, mac, token, name, model, extra=""):
        self.did = did
        self.mac = mac
        self.token = token
        self.name = name
        self.model = model
        self.extra = extra


class XiaomiCloudClient:
    """Xiaomi Cloud API client for password login (used by beaconkey endpoint)."""

    def __init__(self, username: str, password: str, server: str = "cn"):
        self._username = username
        self._password = password
        self._server = server
        self._agent = _generate_agent()
        self._device_id = _generate_device_id()
        self._session = requests.Session()
        self._ssecurity: Optional[str] = None
        self._service_token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._notification_url: Optional[str] = None
        self._context: Optional[str] = None

    def login(self) -> bool:
        self._session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="mi.com")
        self._session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="xiaomi.com")
        self._session.cookies.set("deviceId", self._device_id, domain="mi.com")
        self._session.cookies.set("deviceId", self._device_id, domain="xiaomi.com")
        if not self._login_step_1():
            raise XiaomiCloudLoginError("用户名无效")
        if not self._login_step_2():
            raise XiaomiCloudLoginError("登录或密码错误")
        if not self._login_step_3():
            raise XiaomiCloudLoginError("无法获取 service token")
        return True

    def get_beaconkey(self, did: str, timeout: Optional[float] = None) -> Optional[str]:
        """取 BLE Key。`timeout` 语义与 `QrCodeXiaomiCloudClient.get_beaconkey` 一致。"""
        url = SERVERS.get(self._server, SERVERS["cn"])
        result = self._api_call(f"{url}/v2/device/blt_get_beaconkey", {
            "data": json.dumps({"did": did, "pdid": 1})
        }, timeout=timeout)
        if result and result.get("code") == 0:
            return result.get("result", {}).get("key")
        return None

    def _api_call(self, url, params, timeout=None):
        headers = {"Accept-Encoding": "identity", "User-Agent": self._agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
                    "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"}
        cookies = {"userId": str(self._user_id), "serviceToken": str(self._service_token),
                   "locale": "en_GB", "timezone": "GMT+08:00", "channel": "MI_APP_STORE"}
        millis = round(time.time() * 1000)
        nonce = _generate_nonce(millis)
        sn = _signed_nonce(self._ssecurity, nonce)
        fields = _generate_enc_params(url, "POST", sn, nonce, params, self._ssecurity)
        try:
            response = self._session.post(url, headers=headers, cookies=cookies, params=fields,
                                           timeout=timeout or CLOUD_HTTP_TIMEOUT)
            if response.status_code == 200:
                decoded = _decrypt_rc4(_signed_nonce(self._ssecurity, fields["_nonce"]), response.text)
                return json.loads(decoded)
        except Exception:
            pass
        return None

    def _login_step_1(self):
        url = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        response = self._session.get(url, headers=headers,
                                      cookies={"userId": self._username},
                                      timeout=CLOUD_HTTP_TIMEOUT)
        if response.status_code != 200:
            return False
        json_resp = self._to_json(response.text)
        if "_sign" in json_resp:
            self._sign = json_resp["_sign"]
            return True
        if "ssecurity" in json_resp:
            self._ssecurity = json_resp["ssecurity"]
            self._user_id = json_resp["userId"]
            self._location = json_resp.get("location")
            return True
        return False

    def _login_step_2(self):
        import hashlib as hl
        url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        fields = {"sid": "xiaomiio",
                   "hash": hl.md5(self._password.encode()).hexdigest().upper(),
                   "callback": "https://sts.api.io.mi.com/sts",
                   "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                   "user": self._username,
                   "_sign": getattr(self, "_sign", ""),
                   "_json": "true"}
        response = self._session.post(url, headers=headers, params=fields,
                                       allow_redirects=False, timeout=CLOUD_HTTP_TIMEOUT)
        if response.status_code != 200:
            return False
        json_resp = self._to_json(response.text)
        if json_resp.get("captchaUrl"):
            raise XiaomiCloudLoginError("需要验证码，暂不支持")
        if json_resp.get("notificationUrl"):
            self._notification_url = json_resp["notificationUrl"]
            err = XiaomiCloudLoginError("需要二步验证")
            err.need_2fa = True
            raise err
        if "ssecurity" not in json_resp or len(str(json_resp["ssecurity"])) <= 4:
            return False
        self._ssecurity = json_resp["ssecurity"]
        self._user_id = json_resp.get("userId")
        self._location = json_resp.get("location")
        return True

    def _login_step_3(self):
        if not self._location:
            return True
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        response = self._session.get(self._location, headers=headers, timeout=CLOUD_HTTP_TIMEOUT)
        if response.status_code == 200:
            self._service_token = response.cookies.get("serviceToken")
            return self._service_token is not None
        return False

    @staticmethod
    def _to_json(text):
        try:
            return json.loads(text.replace("&&&START&&&", "").strip())
        except json.JSONDecodeError:
            return {}


class QrCodeXiaomiCloudClient:
    """Xiaomi Cloud API client for QR code login."""

    def __init__(self, server: str = "cn"):
        self._server = server
        self._agent = _generate_agent()
        self._device_id = _generate_device_id()
        self._session = requests.Session()
        self._ssecurity: Optional[str] = None
        self._service_token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._qr_image_url: Optional[str] = None
        self._login_url: Optional[str] = None
        self._long_polling_url: Optional[str] = None
        self._location: Optional[str] = None
        self._timeout: int = 300

    def start_qr_login(self) -> dict:
        """Step 1: Get QR code URL, download image, and start background long-poll."""
        url = "https://account.xiaomi.com/longPolling/loginUrl"
        data = {
            "_qrsize": "480",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false",
            "sid": "xiaomiio",
            "serviceParam": "",
            "_locale": "en_GB",
            "_dc": str(int(time.time() * 1000))
        }
        response = self._session.get(url, params=data)
        if response.status_code != 200:
            raise XiaomiCloudLoginError("获取二维码失败")

        resp_data = self._to_json(response.text)
        if "qr" not in resp_data:
            raise XiaomiCloudLoginError("获取二维码失败: 服务器未返回 QR 数据")

        self._qr_image_url = resp_data["qr"]
        self._login_url = resp_data["loginUrl"]
        self._long_polling_url = resp_data["lp"]
        self._timeout = resp_data.get("timeout", 300)
        _LOGGER.info("QR login: login_url=%s", self._login_url[:120] if self._login_url else "None")

        # Download QR image
        qr_resp = self._session.get(self._qr_image_url)
        qr_base64 = None
        if qr_resp.status_code == 200 and qr_resp.content:
            import base64
            ct = qr_resp.headers.get("Content-Type", "image/png")
            qr_base64 = f"data:{ct};base64," + base64.b64encode(qr_resp.content).decode()

        # Start long-poll immediately in background thread (like original extractor)
        self._poll_result = None
        self._poll_error = None
        self._poll_done = False
        import threading
        t = threading.Thread(target=self._background_poll, daemon=True)
        t.start()

        return {
            "qr_image": qr_base64,
            "login_url": self._login_url,
        }

    def _background_poll(self):
        """Long-poll in background thread, store result."""
        try:
            _LOGGER.info("Background long-poll started on: %s", self._long_polling_url[:80])
            start_time = time.time()
            while True:
                try:
                    response = self._session.get(self._long_polling_url, timeout=10)
                except requests.exceptions.Timeout:
                    elapsed = time.time() - start_time
                    _LOGGER.info("Background long-poll timeout (%.0fs/%ds)", elapsed, self._timeout)
                    if elapsed > self._timeout:
                        self._poll_error = XiaomiCloudLoginError("二维码已过期，请重新获取")
                        self._poll_done = True
                        return
                    continue
                except requests.exceptions.RequestException as e:
                    self._poll_error = XiaomiCloudLoginError(f"网络错误: {e}")
                    self._poll_done = True
                    return

                if response.status_code == 200:
                    _LOGGER.info("Background long-poll succeeded!")
                    resp_data = self._to_json(response.text)
                    self._user_id = resp_data.get("userId")
                    self._ssecurity = resp_data.get("ssecurity")
                    self._location = resp_data.get("location")
                    self._poll_done = True
                    return
                else:
                    _LOGGER.error("Background long-poll failed: %d", response.status_code)
        except Exception as e:
            _LOGGER.error("Background long-poll exception: %s", e)
            self._poll_error = XiaomiCloudLoginError(f"登录异常: {e}")
            self._poll_done = True

    def complete_qr_login(self) -> bool:
        """Step 2: Wait for background long-poll to complete, then get serviceToken."""
        # Already have serviceToken from previous call
        if self._service_token:
            return True

        if not self._poll_done:
            raise XiaomiCloudLoginError("等待扫码中...")

        if self._poll_error:
            raise self._poll_error

        if not self._ssecurity or not self._location:
            raise XiaomiCloudLoginError("登录失败: 未获取到认证信息")

        # Get serviceToken — location URL is one-time-use, only call once
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        _LOGGER.info("Following location URL (one-time)")
        response = self._session.get(self._location, headers=headers)
        _LOGGER.info("Location response: status=%d", response.status_code)
        self._service_token = response.cookies.get("serviceToken")
        if not self._service_token:
            self._service_token = self._session.cookies.get("serviceToken")
        if not self._service_token:
            raise XiaomiCloudLoginError("无法获取 service token")
        return True

    def get_devices(self) -> list:
        """Get all devices from Xiaomi cloud."""
        url = SERVERS.get(self._server, SERVERS["cn"])
        homes = self._api_call(f"{url}/v2/homeroom/gethome", {
            "data": '{"fg": true, "fetch_share": true, "fetch_share_dev": true, "limit": 300, "app_ver": 7}'
        })
        if not homes or homes.get("code") != 0:
            raise XiaomiCloudLoginError(f"获取设备列表失败: {homes}")

        _LOGGER.info("gethome result keys: %s", list(homes.get("result", {}).keys()) if homes.get("result") else "None")

        devices = []
        homelist = homes.get("result", {}).get("homelist", [])
        _LOGGER.info("Found %d homes", len(homelist))
        for home in homelist:
            home_id = home.get("id")
            owner_id = home.get("uid", self._user_id)
            home_data = self._api_call(f"{url}/v2/home/home_device_list", {
                "data": json.dumps({
                    "home_owner": owner_id,
                    "home_id": home_id,
                    "limit": 200,
                    "get_split_device": True,
                    "support_smart_home": True,
                })
            })
            if not home_data or home_data.get("code") != 0:
                _LOGGER.warning("home_device_list failed for home %s: %s", home_id, home_data)
                continue
            result = home_data.get("result", {})
            dev_list = result.get("device_info", result.get("list", []))
            _LOGGER.info("Home %s: found %d devices", home_id, len(dev_list))
            for dev in dev_list:
                token = dev.get("token", "")
                name = dev.get("name", "")
                model = dev.get("model", "")
                if token:
                    devices.append(XiaomiCloudDevice(
                        did=dev.get("did", ""),
                        mac=dev.get("mac", ""),
                        token=token,
                        name=name,
                        model=model,
                    ))
                else:
                    _LOGGER.debug("Skip device %s (no token)", name)
        _LOGGER.info("Total devices with valid token: %d", len(devices))
        return devices

    def get_beaconkey(self, did: str, timeout: Optional[float] = None) -> Optional[str]:
        """Get BLE beacon key for a device.

        `timeout`：本次调用的 HTTP 超时（秒）。批量导入时由调用方按**剩余预算**
        下发——只判"下一发之前还剩多少"挡不住已经飞出去的那一发，最后一条照样能
        跑满它自己的超时，于是总耗时 = 预算 + 一发最坏值，越过 30s 请求上限。
        """
        url = SERVERS.get(self._server, SERVERS["cn"])
        result = self._api_call(f"{url}/v2/device/blt_get_beaconkey", {
            "data": json.dumps({"did": did, "pdid": 1})
        }, timeout=timeout)
        _LOGGER.info("beaconkey response: %s", result)
        if result and result.get("code") == 0:
            key = result.get("result", {}).get("beaconkey", "")
            if key:
                return key
        return None

    def _api_call(self, url, params, timeout=None):
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": self._agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": str(self._user_id),
            "yetAnotherServiceToken": str(self._service_token),
            "serviceToken": str(self._service_token),
            "locale": "en_GB",
            "timezone": "GMT+02:00",
            "is_daylight": "1",
            "dst_offset": "3600000",
            "channel": "MI_APP_STORE",
        }
        millis = round(time.time() * 1000)
        nonce = _generate_nonce(millis)
        sn = _signed_nonce(self._ssecurity, nonce)
        fields = _generate_enc_params(url, "POST", sn, nonce, params, self._ssecurity)
        try:
            response = self._session.post(url, headers=headers, cookies=cookies, params=fields,
                                           timeout=timeout or CLOUD_HTTP_TIMEOUT)
            if response.status_code == 200:
                decoded = _decrypt_rc4(_signed_nonce(self._ssecurity, fields["_nonce"]), response.text)
                return json.loads(decoded)
            _LOGGER.error("API %s returned %d: %s", url.split("/")[-1], response.status_code, response.text[:200])
        except Exception as e:
            _LOGGER.error("API call failed: %s", e)
        return None

    @staticmethod
    def _to_json(text):
        try:
            return json.loads(text.replace("&&&START&&&", "").strip())
        except json.JSONDecodeError:
            return {}
