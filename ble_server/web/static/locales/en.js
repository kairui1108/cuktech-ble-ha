/*!
 * en locale pack for the CUKTECH BLE web UI.
 * Natural, idiomatic English translations (no Chinglish).
 */
(function (global) {
    'use strict';
    global.I18N_RESOURCES = global.I18N_RESOURCES || {};
    global.I18N_RESOURCES['en'] = {
        // ── Common ──
        common: {
            connect: 'Connect',
            disconnect: 'Disconnect',
            restart: 'Restart',
            close: 'Close',
            cancel: 'Cancel',
            set: 'Set',
            clear: 'Clear',
            clearing: 'Clearing...',
            setting: 'Setting...',
            saving: 'Saving...',
            notSet: 'Not set',
            loading: 'Loading...',
            connected: 'Connected',
            connecting: 'Authenticating...',
            connectingDots: 'Connecting...',
            disconnecting: 'Disconnecting...',
            disconnected: 'Not connected',
            unknownError: 'Unknown error',
            networkError: 'Network error: {{msg}}',
            saveFailed: 'Save failed: {{msg}}',
            setFailed: 'Failed to set: {{msg}}',
            firmware: 'Firmware: {{version}}',
            theme: 'Theme',
            logs: 'Logs',
            minutes: { one: '1 min', other: '{{count}} min' }
        },

        // ── Power units ──
        power: {
            total: 'Total Power (W)',
            maxVoltage: 'Max Voltage (V)',
            voltage: 'Voltage (V)',
            current: 'Current (A)',
            power: 'Power (W)'
        },

        // ── Index page ──
        index: {
            connectionStatus: 'Connection Status',
            bleControl: 'BLE Control',
            powerChart: 'Power Chart',
            portMonitor: 'Port Monitor',
            clickForDetail: '(click for details)',
            chargeHistory: 'Charge History',
            deviceSettings: 'Device Settings',
            config: 'Config',
            themeDark: 'Dark',
            themeDeepBlue: 'Deep Blue',
            themeOcean: 'Ocean',
            themeGray: 'Gray',
            themeLight: 'Light',
            themeSystem: 'System',
            range30: '30m',
            range60: '60m',
            range90: '90m',
            range120: '120m',
            range1440: '24h'
        },

        // ── Scene modes (device) ──
        scene: {
            ai: 'AI Mode',
            eco: 'Digital Ecosystem',
            single: 'Single Port',
            balanced: 'Balanced Mode',
            descAi: 'Automatically detects the connected device and picks the optimal charging power',
            descEco: 'Charges multiple ports at once with balanced power distribution',
            descSingle: 'Maximum power output from a single port, prioritizing C1',
            descBalanced: 'Balances charging power across all ports'
        },

        // ── Device settings (PIID config) ──
        settings: {
            sceneMode: 'Scene Mode',
            screenTimeout: 'Screen-Off Time',
            deviceLanguage: 'Language',
            usbATrickle: 'USB-A Trickle Charge',
            idleScreenOff: 'Idle Screen-Off',
            screenLock: 'Screen Orientation Lock',
            off: 'Off',
            on: 'On',
            min5: '5 min',
            min10: '10 min',
            min30: '30 min',
            alwaysOn: 'Always On',
            min1: '1 min'
        },

        // ── Port detail modal ──
        modal: {
            portDetail: '{{port}} Port Details',
            voltage: 'Voltage (V)',
            current: 'Current (A)',
            power: 'Power (W)',
            protocol: 'Live Protocol',
            realtime: '⚡ Live',
            realtimeStop: '⏹ Live',
            protocolSwitch: 'Protocol Toggle',
            noData: 'Protocol Toggle — no data',
            ppsNote: 'PPS is also disabled when PD is off',
            replugNote: 'Replug the port to apply'
        },

        // ── Connection quality tooltips ──
        quality: {
            connectionDuration: 'Connection time',
            lastPush: 'Last push',
            nextReconnect: 'Next reconnect',
            decryptSuccess: 'Decrypt success',
            notifyResponse: 'Notify response',
            connectionStable: 'Stability',
            reconnect5m: 'Reconnects (5m)',
            runtime: 'Uptime',
            disconnects: 'Disconnects',
            publishFailures: 'Publish failures',
            pingLost: 'Ping lost',
            reconnectCount: 'Reconnects',
            notConnected: 'Not connected',
            none: 'None',
            secondsAgo: '{{count}}s ago',
            secondsLater: 'in {{count}}s',
            times: '{{count}}×'
        },

        // ── Countdown ──
        countdown: {
            title: 'Countdown',
            placeholder: 'min',
            quick: '{{count}}m'
        },

        // ── Charge history ──
        charge: {
            today: 'Today',
            yesterday: 'Yesterday',
            week: 'This Week',
            month: 'This Month',
            all: 'All',
            totalWh: 'Total Energy (Wh)',
            sessionCount: 'Sessions',
            avgPower: 'Avg Power (W)',
            peakPower: 'Peak Power (W)',
            noRecords: 'No charge records yet',
            energy: 'Energy: {{wh}} Wh',
            powerTooltip: 'Power: {{power}} W',
            protocolTooltip: 'Protocol: {{protocol}}',
            prevPage: 'Prev',
            nextPage: 'Next',
            accuracy: 'Precision',
            allPoints: 'All',
            points100: '100 pts',
            points200: '200 pts',
            points300: '300 pts',
            points600: '600 pts',
            energyUnit: 'Energy (Wh)',
            avgPowerUnit: 'Avg Power (W)',
            peakPowerUnit: 'Peak Power (W)',
            avgVoltageUnit: 'Avg Voltage (V)',
            avgCurrentUnit: 'Avg Current (A)',
            yesterdayTime: 'Yesterday {{time}}'
        },

        // ── Phone page ──
        phone: {
            sceneMode: 'Scene Mode',
            portControl: 'Port Control',
            screenTimeout: 'Screen-Off Time',
            usbATrickle: 'USB-A Trickle Charge',
            totalPowerTitle: 'Current Total Power',
            currentPower: 'Current Power',
            powerChart: 'Power Chart',
            powerDist: 'Power Distribution',
            chargeHistory: 'Charge History',
            delayOff: 'Delay Off',
            noActivePorts: 'No active ports',
            connectToast: 'Connecting to the device, please wait...',
            replugNote: 'Replug the port to apply'
        },

        // ── Config page ──
        config: {
            title: 'System Config',
            bleDevice: 'BLE Device',
            xiaomiAuto: 'Get from Xiaomi Cloud',
            mac: 'MAC Address',
            macHint: 'Charger BLE MAC address',
            tokenHint: 'Device auth Token (hex)',
            bleKeyHint: 'Encryption key (hex)',
            mqttSection: 'MQTT (Home Assistant)',
            enableMqtt: 'Enable MQTT',
            server: 'Server',
            serverHint: 'MQTT Broker address',
            port: 'Port',
            username: 'Username',
            password: 'Password',
            optional: 'optional',
            topicPrefix: 'Topic Prefix',
            topicHint: 'MQTT topic prefix',
            bemfaSection: 'Bemfa Cloud',
            bemfaRegister: 'Register to get your private key',
            enableBemfa: 'Enable Bemfa Cloud',
            privateKey: 'Private Key',
            privateKeyHint: 'Your Bemfa Cloud private key',
            placeholderUid: 'Bemfa Cloud private key',
            portName: '{{port}} Port Name',
            portNameHint: 'Device display name on Bemfa',
            bleStatusName: 'BLE Status Name',
            serverSection: 'Server',
            webPort: 'Web Port',
            retention: 'Data retention (days)',
            webLanguage: 'Interface Language',
            webLanguageHint: 'Applies immediately, no restart needed; all pages stay in sync',
            langAuto: 'Follow System',
            langApplied: 'Language switched',
            langAutoApplied: 'Now following the system language',
            sessionRecording: 'Charge Session Recording',
            sessionRecordingHint: 'Takes effect immediately, no restart needed; history stops being recorded when off',
            currentStatus: 'Status',
            loaded: 'Config loaded',
            loadFailed: 'Failed to load: {{msg}}',
            saveRestart: 'Save & Restart',
            savedRestart: 'Config saved, service is restarting...',
            sessionRecordingOn: 'Charge session recording enabled',
            sessionRecordingOff: 'Charge session recording disabled',
            xiaomiLogin: 'Xiaomi Cloud Login',
            serverRegion: 'Server Region',
            regionCn: 'Mainland China (cn)',
            regionDe: 'Europe (de)',
            regionUs: 'United States (us)',
            regionRu: 'Russia (ru)',
            regionTw: 'Taiwan (tw)',
            regionSg: 'Singapore (sg)',
            regionIn: 'India (in)',
            getQR: 'Get QR Code',
            fetching: 'Fetching...',
            connectingXiaomi: 'Connecting to Xiaomi Cloud...',
            qrFailed: 'Failed to get QR code',
            qrLoadFailed: 'Failed to load QR code',
            waitingScan: 'Waiting for scan...',
            scanInstruction: 'Scan the QR code with the Mi Home app to log in',
            scanWithApp: 'Scan the QR code below with the <strong style="color:#333;">Mi Home App</strong>',
            orOpenLink: 'Or copy the link and open it in your browser:',
            scanned: "I've scanned the code",
            scanTimeout: 'Timed out waiting — please get a new QR code',
            noDevices: 'No devices found',
            selectCharger: 'Select your charger:',
            fetchingBleKey: 'Fetching BLE Key...',
            bleKeyFailed: 'Failed to get BLE Key: {{msg}}',
            deviceInfoGot: 'Device info retrieved — please review and save',
            placeholderC1: 'USB-C 1 switch',
            placeholderC2: 'USB-C 2 switch',
            placeholderC3: 'USB-C 3 switch',
            placeholderA: 'USB-A switch',
            placeholderBle: 'BLE switch'
        },

        // ── Device info (HA iframe) ──
        deviceInfo: {
            activePorts: 'Active Ports'
        },

        // ── Page titles ──
        pageTitle: {
            index: 'CUKTECH 10 GaN Charger Ultra',
            phone: 'CUKTECH 10 Ultra Charger',
            config: 'CUKTECH Config'
        }
    };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));