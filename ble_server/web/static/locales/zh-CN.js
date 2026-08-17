/*!
 * zh-CN locale pack for the CUKTECH BLE web UI.
 * Values reproduce the original Chinese UI exactly.
 */
(function (global) {
    'use strict';
    global.I18N_RESOURCES = global.I18N_RESOURCES || {};
    global.I18N_RESOURCES['zh-CN'] = {
        // ── Common ──
        common: {
            connect: '连接设备',
            disconnect: '断开设备',
            restart: '重启',
            close: '关闭',
            cancel: '取消',
            set: '设置',
            clear: '清除',
            clearing: '清除中...',
            setting: '设置中...',
            saving: '保存中...',
            notSet: '未设置',
            loading: '加载中...',
            connected: '已连接',
            connecting: '认证中...',
            connectingDots: '连接中...',
            disconnecting: '断开中...',
            disconnected: '未连接',
            unknownError: '未知错误',
            networkError: '网络错误: {{msg}}',
            saveFailed: '保存失败: {{msg}}',
            setFailed: '设置失败: {{msg}}',
            firmware: '固件版本：{{version}}',
            theme: '主题',
            logs: '日志',
            minutes: '{{count}}分钟'
        },

        // ── Power units ──
        power: {
            total: '总功率 (W)',
            maxVoltage: '最高电压 (V)',
            voltage: '电压 V',
            current: '电流 A',
            power: '功率 W'
        },

        // ── Index page ──
        index: {
            connectionStatus: '连接状态',
            bleControl: 'BLE 控制',
            powerChart: '功率曲线',
            portMonitor: '端口监控',
            clickForDetail: '(点击查看详情)',
            chargeHistory: '充电记录',
            deviceSettings: '设备设置',
            config: '配置',
            themeDark: '暗色',
            themeDeepBlue: '深蓝',
            themeOcean: '海洋',
            themeGray: '灰色',
            themeLight: '浅色',
            themeSystem: '跟随系统',
            range30: '30分',
            range60: '60分',
            range90: '90分',
            range120: '120分',
            range1440: '24小时'
        },

        // ── Scene modes (device) ──
        scene: {
            ai: 'AI模式',
            eco: '数码生态',
            single: '单口模式',
            balanced: '均衡模式',
            descAi: '自动识别设备智能匹配最优充电功率',
            descEco: '多口同时充电均衡分配功率',
            descSingle: '单口最大功率输出优先C1口',
            descBalanced: '多个端口均衡分配充电功率'
        },

        // ── Device settings (PIID config) ──
        settings: {
            sceneMode: '场景模式',
            screenTimeout: '息屏时间',
            deviceLanguage: '语言',
            usbATrickle: 'USB-A小电流',
            idleScreenOff: '空闲息屏',
            screenLock: '屏幕方向锁',
            off: '关闭',
            on: '开启',
            min5: '5分钟',
            min10: '10分钟',
            min30: '30分钟',
            alwaysOn: '常亮',
            min1: '1分钟'
        },

        // ── Port detail modal ──
        modal: {
            portDetail: '{{port}} 端口详情',
            voltage: '电压 (V)',
            current: '电流 (A)',
            power: '功率 (W)',
            protocol: '实时充电协议',
            realtime: '⚡ 实时',
            realtimeStop: '⏹ 实时',
            protocolSwitch: '协议开关',
            noData: '协议开关 — 暂无数据',
            ppsNote: '关闭PD后PPS也将关闭',
            replugNote: '需重新插拔端口设备生效'
        },

        // ── Connection quality tooltips ──
        quality: {
            connectionDuration: '连接时长',
            lastPush: '最后推送',
            nextReconnect: '下次重连',
            decryptSuccess: '解密成功',
            notifyResponse: '通知响应',
            connectionStable: '连接稳定',
            reconnect5m: '5min重连',
            runtime: '运行时长',
            disconnects: '断连次数',
            publishFailures: '发送失败',
            pingLost: 'Ping丢包',
            reconnectCount: '重连次数',
            notConnected: '未连接',
            none: '无',
            secondsAgo: '{{count}}s前',
            secondsLater: '{{count}}s后',
            times: '{{count}}次'
        },

        // ── Countdown ──
        countdown: {
            title: '倒计时设置',
            placeholder: '分钟',
            quick: '{{count}}分'
        },

        // ── Charge history ──
        charge: {
            today: '今日',
            yesterday: '昨日',
            week: '本周',
            month: '本月',
            all: '全部',
            totalWh: '总充电 Wh',
            sessionCount: '充电次数',
            avgPower: '平均功率 W',
            peakPower: '峰值功率 W',
            noRecords: '暂无充电记录',
            energy: '电量：{{wh}}Wh',
            powerTooltip: '功率: {{power}}W',
            protocolTooltip: '协议: {{protocol}}',
            prevPage: '上一页',
            nextPage: '下一页',
            accuracy: '精度',
            allPoints: '全部',
            points100: '100点',
            points200: '200点',
            points300: '300点',
            points600: '600点',
            energyUnit: '电量 Wh',
            avgPowerUnit: '均功率 W',
            peakPowerUnit: '峰功率 W',
            avgVoltageUnit: '均电压 V',
            avgCurrentUnit: '均电流 A',
            yesterdayTime: '昨天 {{time}}'
        },

        // ── Phone page ──
        phone: {
            sceneMode: '场景模式',
            portControl: '端口控制',
            screenTimeout: '息屏时间',
            usbATrickle: 'USB-A小电流',
            totalPowerTitle: '当前总功率',
            currentPower: '当前功率',
            powerChart: '功率曲线',
            powerDist: '功率占比',
            chargeHistory: '充电记录',
            delayOff: '延时关闭',
            noActivePorts: '暂无活跃端口',
            connectToast: '正在连接设备，请稍候...',
            replugNote: '需重新插拔端口'
        },

        // ── Config page ──
        config: {
            title: '系统配置',
            bleDevice: 'BLE 设备',
            xiaomiAuto: '小米云自动获取',
            mac: 'MAC 地址',
            macHint: '充电器蓝牙 MAC 地址',
            tokenHint: '设备认证 Token (十六进制)',
            bleKeyHint: '加密密钥 (十六进制)',
            mqttSection: 'MQTT (Home Assistant)',
            enableMqtt: '启用 MQTT',
            server: '服务器',
            serverHint: 'MQTT Broker 地址',
            port: '端口',
            username: '用户名',
            password: '密码',
            optional: '可选',
            topicPrefix: 'Topic 前缀',
            topicHint: 'MQTT 主题前缀',
            bemfaSection: '巴法云',
            bemfaRegister: '注册获取私钥',
            enableBemfa: '启用巴法云',
            privateKey: '私钥',
            privateKeyHint: '巴法云用户 私钥',
            placeholderUid: '巴法云 私钥',
            portName: '{{port}} 端口名称',
            portNameHint: '巴法云设备显示名',
            bleStatusName: '蓝牙状态名称',
            serverSection: '服务器',
            webPort: 'Web 端口',
            retention: '数据保留天数',
            webLanguage: '界面语言',
            webLanguageHint: '即时生效，无需重启；所有页面自动同步',
            langAuto: '跟随系统',
            langApplied: '语言已切换',
            langAutoApplied: '已切换到跟随系统',
            sessionRecording: '充电会话记录',
            sessionRecordingHint: '即时生效，无需重启；关闭后不再记录充电历史',
            currentStatus: '当前状态',
            loaded: '配置已加载',
            loadFailed: '加载失败: {{msg}}',
            saveRestart: '保存配置并重启',
            savedRestart: '配置已保存，服务正在重启...',
            sessionRecordingOn: '已开启充电会话记录',
            sessionRecordingOff: '已关闭充电会话记录',
            xiaomiLogin: '小米云登录',
            serverRegion: '服务器区域',
            regionCn: '中国大陆 (cn)',
            regionDe: '欧洲 (de)',
            regionUs: '美国 (us)',
            regionRu: '俄罗斯 (ru)',
            regionTw: '台湾 (tw)',
            regionSg: '新加坡 (sg)',
            regionIn: '印度 (in)',
            getQR: '获取二维码',
            fetching: '获取中...',
            connectingXiaomi: '正在连接小米云...',
            qrFailed: '获取二维码失败',
            qrLoadFailed: '二维码加载失败',
            waitingScan: '等待扫码...',
            scanInstruction: '请用米家 App 扫描二维码登录',
            scanWithApp: '使用<strong style="color:#333;">米家 App</strong> 扫描下方二维码',
            orOpenLink: '或复制链接到浏览器打开：',
            scanned: '已完成扫码',
            scanTimeout: '等待超时，请重新获取二维码',
            noDevices: '未找到设备',
            selectCharger: '选择充电器设备：',
            fetchingBleKey: '正在获取 BLE Key...',
            bleKeyFailed: '获取 BLE Key 失败: {{msg}}',
            deviceInfoGot: '已获取设备信息，请检查后保存',
            placeholderC1: 'C口1开关',
            placeholderC2: 'C口2开关',
            placeholderC3: 'C口3开关',
            placeholderA: 'USB-A开关',
            placeholderBle: '蓝牙开关'
        },

        // ── Device info (HA iframe) ──
        deviceInfo: {
            activePorts: '活跃端口'
        },

        // ── Page titles ──
        pageTitle: {
            index: 'CUKTECH 10 GaN Charger Ultra',
            phone: 'CUKTECH 10 Ultra 充电器',
            config: 'CUKTECH 配置'
        }
    };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
