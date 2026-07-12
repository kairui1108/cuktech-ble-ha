        // Theme definitions
        const THEMES = {
            'ha-dark': {
                '--bg': '#1c1c1c', '--card-bg': '#252525', '--card-border': '#3b3b3b',
                '--text': '#e1e1e1', '--text-dim': '#959595',
                '--accent': '#03a9f4', '--accent-rgb': '3, 169, 244',
                '--success': '#389e3d', '--success-rgb': '56, 158, 61',
                '--warning': '#ffa42b', '--warning-rgb': '255, 164, 43',
                '--danger': '#db4437', '--danger-rgb': '219, 68, 55'
            },
            'deep-blue': {
                '--bg': '#0f0f1a', '--card-bg': '#1a1a2e', '--card-border': '#2a2a4a',
                '--text': '#e8e8f0', '--text-dim': '#8888aa',
                '--accent': '#00d4ff', '--accent-rgb': '0, 212, 255',
                '--success': '#00e676', '--success-rgb': '0, 230, 118',
                '--warning': '#ffc107', '--warning-rgb': '255, 193, 7',
                '--danger': '#ff5252', '--danger-rgb': '255, 82, 82'
            },
            'ocean': {
                '--bg': '#0a1628', '--card-bg': '#0f2035', '--card-border': '#1a3a5c',
                '--text': '#e0f0ff', '--text-dim': '#7aa3cc',
                '--accent': '#00b4d8', '--accent-rgb': '0, 180, 216',
                '--success': '#48bb78', '--success-rgb': '72, 187, 120',
                '--warning': '#f6ad55', '--warning-rgb': '246, 173, 85',
                '--danger': '#fc8181', '--danger-rgb': '252, 129, 129'
            },
            'gray': {
                '--bg': '#2d2d2d', '--card-bg': '#3a3a3a', '--card-border': '#4a4a4a',
                '--text': '#f0f0f0', '--text-dim': '#aaaaaa',
                '--accent': '#4fc3f7', '--accent-rgb': '79, 195, 247',
                '--success': '#81c784', '--success-rgb': '129, 199, 132',
                '--warning': '#ffb74d', '--warning-rgb': '255, 183, 77',
                '--danger': '#e57373', '--danger-rgb': '229, 115, 115'
            },
            'light': {
                '--bg': '#f5f5f5', '--card-bg': '#ffffff', '--card-border': '#e0e0e0',
                '--text': '#212121', '--text-dim': '#757575',
                '--accent': '#1976d2', '--accent-rgb': '25, 118, 210',
                '--success': '#388e3c', '--success-rgb': '56, 142, 60',
                '--warning': '#f57c00', '--warning-rgb': '245, 124, 0',
                '--danger': '#d32f2f', '--danger-rgb': '211, 47, 47'
            }
        };

        function setTheme(themeName) {
            if (themeName === 'system') {
                localStorage.setItem('cuktech-theme', 'system');
                const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                applyTheme(prefersDark ? 'ha-dark' : 'light');
            } else {
                localStorage.setItem('cuktech-theme', themeName);
                localStorage.removeItem('cuktech-theme-original');
                applyTheme(themeName);
            }
            document.querySelectorAll('.theme-option').forEach(opt => {
                opt.classList.toggle('active', opt.dataset.theme === themeName);
            });
            document.getElementById('themeMenu').classList.remove('show');
        }

        function applyTheme(themeName) {
            const theme = THEMES[themeName];
            if (!theme) return;
            const root = document.documentElement;
            Object.entries(theme).forEach(([key, value]) => {
                root.style.setProperty(key, value);
            });
        }

        function toggleThemeMenu() {
            document.getElementById('themeMenu').classList.toggle('show');
        }

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.theme-switcher')) {
                document.getElementById('themeMenu').classList.remove('show');
            }
        });

        // Load saved theme
        const savedTheme = localStorage.getItem('cuktech-theme') || 'ha-dark';
        setTimeout(() => setTheme(savedTheme), 0);

        // Log level management
        async function setLogLevel(level) {
            try {
                await fetch(`${API_BASE}/api/log-level`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ level })
                });
                localStorage.setItem('cuktech-log-level', level);
                document.querySelectorAll('#logLevelMenu .theme-option').forEach(opt => {
                    opt.classList.toggle('active', opt.dataset.level === level);
                });
                document.getElementById('logLevelMenu').classList.remove('show');
            } catch (e) {
                console.error('Failed to set log level:', e);
            }
        }

        async function initLogLevel() {
            try {
                const res = await fetch(`${API_BASE}/api/log-level`);
                const data = await res.json();
                if (data.level) {
                    localStorage.setItem('cuktech-log-level', data.level);
                    document.querySelectorAll('#logLevelMenu .theme-option').forEach(opt => {
                        opt.classList.toggle('active', opt.dataset.level === data.level);
                    });
                }
            } catch (e) {
                // Fallback to localStorage
                const saved = localStorage.getItem('cuktech-log-level') || 'info';
                document.querySelectorAll('#logLevelMenu .theme-option').forEach(opt => {
                    opt.classList.toggle('active', opt.dataset.level === saved);
                });
            }
        }

        function toggleLogLevelMenu() {
            document.getElementById('logLevelMenu').classList.toggle('show');
        }

        document.addEventListener('click', (e) => {
            if (!e.target.closest('#logLevelSwitcher')) {
                document.getElementById('logLevelMenu').classList.remove('show');
            }
        });

        // Initialize log level from server
        setTimeout(() => initLogLevel(), 0);

        const API_BASE = window.location.origin;
        const PORT_MAP = { 1: 'C1', 2: 'C2', 3: 'C3', 4: 'A' };
        const PORT_KEY_MAP = { 1: 'c1', 2: 'c2', 3: 'c3', 4: 'a' };

        const SETTINGS_CONFIG = [
            { piid: 5, name: '场景模式', options: [{ value: 1, label: 'AI模式' }, { value: 2, label: '数码生态' }, { value: 3, label: '单口模式' }, { value: 4, label: '均衡模式' }] },
            { piid: 6, name: '息屏时间', options: [{ value: 0, label: '5分钟' }, { value: 1, label: '1分钟' }, { value: 2, label: '10分钟' }, { value: 3, label: '30分钟' }, { value: 4, label: '常亮' }, { value: 5, label: '1分钟' }] },
            { piid: 13, name: '语言', options: [{ value: 0, label: 'English' }, { value: 1, label: '中文' }] },
            { piid: 15, name: 'USB-A常通电', options: [{ value: 0, label: '关闭' }, { value: 1, label: '开启' }] },
            { piid: 19, name: '空闲息屏', options: [{ value: 0, label: '关闭' }, { value: 1, label: '开启' }] },
            { piid: 20, name: '屏幕方向锁', options: [{ value: 0, label: '关闭' }, { value: 1, label: '开启' }] }
        ];

        let lastSettings = {};
        let powerChart = null, modalChart = null, currentModalPort = null, latestPorts = {};
        let bleConnected = false;
        let protocolSwitches = {};
        const portHistory = {
            1: { voltage: [], current: [], power: [], protocol: [] },
            2: { voltage: [], current: [], power: [], protocol: [] },
            3: { voltage: [], current: [], power: [], protocol: [] },
            4: { voltage: [], current: [], power: [], protocol: [] }
        };

        function setTimeRange(minutes) {
            setCurrentHours(minutes / 60);
            localStorage.setItem('cuktech-chart-hours', minutes);
            document.querySelectorAll('.time-btn').forEach(btn => {
                const btnMinutes = btn.textContent === '24小时' ? 1440 : parseInt(btn.textContent);
                btn.classList.toggle('active', btnMinutes === minutes);
            });
            fetchChartData();
        }

        const COUNTDOWN_PIIDS = { 1: 9, 2: 10, 3: 11, 4: 12 };
        const PORT_KEY_TO_ID = { 'c1': 1, 'c2': 2, 'c3': 3, 'a': 4 };
        const QUICK_MINUTES = [15, 30, 60, 90, 120, 240];

        function initChart() {
            const cs = getComputedStyle(document.documentElement);
            const c1 = cs.getPropertyValue('--port-c1').trim() || '#03a9f4';
            const c2 = cs.getPropertyValue('--port-c2').trim() || '#7c4dff';
            const c3 = cs.getPropertyValue('--port-c3').trim() || '#389e3d';
            const ca = cs.getPropertyValue('--port-a').trim() || '#ffa42b';
            const textColor = cs.getPropertyValue('--text').trim() || '#e1e1e1';
            const accentColor = cs.getPropertyValue('--accent').trim() || '#03a9f4';
            const ctx = document.getElementById('powerChart').getContext('2d');
            powerChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [
                        { label: 'C1', data: [], borderColor: c1, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false },
                        { label: 'C2', data: [], borderColor: c2, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false },
                        { label: 'C3', data: [], borderColor: c3, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false },
                        { label: 'A', data: [], borderColor: ca, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false },
                        { label: 'Total', data: [], borderColor: textColor, borderWidth: 2.5, tension: 0.4, pointRadius: 0, fill: false, borderDash: [5, 3] },
                    ]
                },
                options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 }, interaction: { intersect: false, mode: 'index' },
                    plugins: { legend: { display: true, position: 'top', labels: { color: textColor, font: { size: 11 }, boxWidth: 12, padding: 12 } } },
                    scales: { x: { display: true, grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#666', maxTicksLimit: 8, font: { size: 10 } } }, y: { display: true, grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#666', font: { size: 10 } }, beginAtZero: true } }
                }
            });
        }

        async function fetchChartData() {
            try {
                const interval = getInterval();
                const res = await fetch(`${API_BASE}/api/chart?hours=${getCurrentHours()}&interval=${interval}`);
                if (res.status === 304) return;
                if (!res.ok) return;
                const result = await res.json();
                if (result.ok) updateChart(result);
            } catch (e) {
                console.error('Failed to fetch chart data:', e);
            }
        }

        function updateChart(data) {
            powerChart.data.labels = data.labels;
            const power = data.datasets.power;
            for (let i = 0; i < power.length; i++) {
                powerChart.data.datasets[i].data = power[i].data;
            }
            if (power.length >= 5) {
                powerChart.data.datasets[4].data = power[4].data;
            }
            for (let port = 1; port <= 4; port++) {
                portHistory[port].power = power[port - 1].data.slice();
                portHistory[port].voltage = data.datasets.voltage[port - 1].data.slice();
                portHistory[port].current = data.datasets.current[port - 1].data.slice();
                portHistory[port].protocol = power[port - 1].data.map(() => 'idle');
            }
            powerChart.update('none');
        }

        function initModalChart() {
            if (modalChart) modalChart.destroy();
            const colors = getChartColors();
            const ctx = document.getElementById('modalChart').getContext('2d');
            modalChart = new Chart(ctx, {
                type: 'line',
                data: { labels: [], datasets: [
                    { label: '电压 (V)', data: [], borderColor: colors.c1, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false, yAxisID: 'y' },
                    { label: '电流 (A)', data: [], borderColor: colors.c3, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false, yAxisID: 'y' },
                    { label: '功率 (W)', data: [], borderColor: colors.a, borderWidth: 2, tension: 0.4, pointRadius: 0, fill: false, yAxisID: 'y1' },
                ]},
                options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 }, interaction: { intersect: false, mode: 'index' },
                    plugins: { legend: { display: true, position: 'top', labels: { color: colors.textDim, font: { size: 11 }, boxWidth: 12, padding: 12 } } },
                    scales: { x: { display: true, grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: '#666', maxTicksLimit: 8, font: { size: 10 } } },
                        y: { type: 'linear', display: true, position: 'left', grid: { color: 'rgba(255,255,255,0.04)' }, ticks: { color: colors.c1, font: { size: 10 } }, beginAtZero: true, title: { display: true, text: 'V / A', color: colors.textDim } },
                        y1: { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: colors.a, font: { size: 10 } }, beginAtZero: true, title: { display: true, text: 'W', color: colors.textDim } }
                    }
                }
            });
        }

        function openModal(portId) {
            currentModalPort = portId;
            const portKey = PORT_KEY_MAP[portId];
            document.getElementById('modalTitle').textContent = `${PORT_MAP[portId]} 端口详情`;
            document.getElementById('modalTitle').style.color = `var(--port-${portKey})`;
            initModalChart();
            updateModalChart();
            renderProtoSwitches(portKey);
            document.getElementById('portModal').classList.add('show');
        }

        function closeModal() { document.getElementById('portModal').classList.remove('show'); currentModalPort = null; }

        const PROTOCOL_DEFS = {
            c1: [{ key: 'pd', name: 'PD' }, { key: 'pps', name: 'PPS' }, { key: 'ufcs', name: 'UFCS' }],
            c2: [{ key: 'pd', name: 'PD' }, { key: 'pps', name: 'PPS' }, { key: 'ufcs', name: 'UFCS' }],
            c3: [{ key: 'ufcs', name: 'UFCS' }, { key: 'scp', name: 'SCP' }],
            a:  [{ key: 'ufcs', name: 'UFCS' }, { key: 'scp', name: 'SCP' }]
        };
        const PROTOCOL_PENDING = {};

        function renderProtoSwitches(portKey) {
            const grid = document.getElementById('modalProtoSwitches');
            if (!grid) return;
            const defs = PROTOCOL_DEFS[portKey] || [];
            const sw = protocolSwitches[portKey] || {};
            let html = '';
            defs.forEach(p => {
                const on = sw[p.key] || false;
                html += `
                    <div class="proto-switch-item">
                        <span class="proto-switch-label">${p.name}</span>
                        <label class="proto-switch-toggle" id="proto-toggle-${portKey}-${p.key}">
                            <input type="checkbox" ${on ? 'checked' : ''}
                                   onchange="toggleProtocol('${portKey}', '${p.key}', this.checked)">
                            <span class="toggle-slider"></span>
                        </label>
                    </div>`;
            });
            grid.innerHTML = html;
        }

        async function toggleProtocol(port, proto, on) {
            const toggle = document.getElementById(`proto-toggle-${port}-${proto}`);
            if (toggle) toggle.classList.add('pending');
            try {
                const res = await fetch(`${API_BASE}/api/protocol`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ port, protocol: proto, action: on ? 'on' : 'off' })
                });
                const result = await res.json();
                if (result.ok) {
                    // Update local state immediately for responsive UI
                    if (!protocolSwitches[port]) protocolSwitches[port] = {};
                    protocolSwitches[port][proto] = on;
                    // Re-render to sync with potentially updated server state
                    setTimeout(() => fetchStatus(), 500);
                } else {
                    // Revert on failure
                    if (toggle) {
                        const input = toggle.querySelector('input');
                        if (input) input.checked = !on;
                    }
                }
            } catch (e) {
                console.error('Protocol toggle error:', e);
                if (toggle) {
                    const input = toggle.querySelector('input');
                    if (input) input.checked = !on;
                }
            } finally {
                if (toggle) toggle.classList.remove('pending');
            }
        }

        function updateModalChart() {
            if (!currentModalPort || !modalChart) return;
            const h = portHistory[currentModalPort];
            modalChart.data.labels = [...powerChart.data.labels.slice(-h.voltage.length)];
            modalChart.data.datasets[0].data = [...h.voltage];
            modalChart.data.datasets[1].data = [...h.current];
            modalChart.data.datasets[2].data = [...h.power];
            modalChart.update('none');
            document.getElementById('modalVoltage').textContent = (h.voltage[h.voltage.length - 1] || 0).toFixed(1);
            document.getElementById('modalCurrent').textContent = (h.current[h.current.length - 1] || 0).toFixed(1);
            document.getElementById('modalPower').textContent = (h.power[h.power.length - 1] || 0).toFixed(1);
            const protocolEl = document.getElementById('modalProtocol');
            if (protocolEl) {
                const portData = latestPorts[currentModalPort];
                const lastProtocol = portData ? portData.protocol : 'idle';
                protocolEl.textContent = lastProtocol;
                protocolEl.style.color = lastProtocol !== 'idle' ? 'var(--accent)' : 'var(--text-dim)';
            }
        }

        document.getElementById('portModal').addEventListener('click', function(e) { if (e.target === this) closeModal(); });
        document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeModal(); });

        async function fetchStatus() {
            try {
                const res = await fetch(`${API_BASE}/api/status`);
                const data = await res.json();
                updateUI(data);
            } catch (e) { console.error('Fetch error:', e); }
        }

        function updateUI(data) {
            bleConnected = data.connected && data.authenticated;
            latestPorts = data.ports || {};
            protocolSwitches = data.protocol_switches || {};
            updateStatusBadge(data.connected, data.authenticated, data.mqtt_connected);
            updateBleButton();
            renderPorts(data.ports);
            updateSettingsUI(data.settings || {});
            renderCountdown(data.settings || {});
            let totalPower = 0, activeCount = 0, maxV = 0;
            for (const [id, port] of Object.entries(data.ports)) {
                if ((port.current > 0 || port.power > 0) && port.enabled) {
                    totalPower += port.power;
                    activeCount++;
                    maxV = Math.max(maxV, port.voltage);
                }
            }
            document.getElementById('totalPower').textContent = totalPower.toFixed(1);
            document.getElementById('activePorts').textContent = activeCount;
            document.getElementById('maxVoltage').textContent = maxV.toFixed(1);
            if (data.firmware_version) {
                document.getElementById('firmwareVersion').textContent = '固件版本：' + data.firmware_version;
            }
            fetchChartData();
            if (currentModalPort) updateModalChart();
        }

        function updateStatusBadge(connected, authenticated, mqttConnected) {
            const badge = document.getElementById('statusBadge');
            badge.className = (connected && authenticated) ? 'status-badge connected' : 'status-badge disconnected';

            const mqttBadge = document.getElementById('mqttBadge');
            mqttBadge.className = mqttConnected ? 'status-badge connected' : 'status-badge disconnected';
        }

        function updateBleButton() {
            const btn = document.getElementById('bleToggle');
            if (bleConnected) {
                btn.textContent = '断开设备';
                btn.className = 'btn btn-danger';
            } else {
                btn.textContent = '连接设备';
                btn.className = 'btn btn-primary';
            }
        }

        function renderPorts(ports) {
            const grid = document.getElementById('portGrid');
            let html = '';
            for (const [id, name] of Object.entries(PORT_MAP)) {
                const port = ports[id] || { voltage: 0, current: 0, power: 0, enabled: false, protocol: 'idle' };
                const key = PORT_KEY_MAP[id];
                const protocolColor = port.protocol !== 'idle' ? 'var(--accent)' : 'var(--text-dim)';
                html += `
                    <div class="port-card ${port.enabled ? 'active' : ''}" id="port-${id}" onclick="handlePortClick(event, ${id})">
                        <div class="port-header">
                            <span class="port-name ${key}">${name}</span>
                            <label class="port-toggle" onclick="event.stopPropagation()">
                                <input type="checkbox" id="toggle-${key}" ${port.enabled ? 'checked' : ''} onchange="togglePort('${key}', this.checked)">
                                <span class="toggle-slider"></span>
                            </label>
                        </div>
                        <div class="port-stats">
                            <div class="port-stat"><div class="port-stat-value">${port.voltage.toFixed(1)}</div><div class="port-stat-label">电压 V</div></div>
                            <div class="port-stat"><div class="port-stat-value">${port.current.toFixed(1)}</div><div class="port-stat-label">电流 A</div></div>
                            <div class="port-stat"><div class="port-stat-value">${port.power.toFixed(1)}</div><div class="port-stat-label">功率 W</div></div>
                        </div>
                        <div style="text-align:center;margin-top:8px;font-size:11px;color:${protocolColor}">${port.protocol}</div>
                    </div>`;
            }
            grid.innerHTML = html;
        }

        function handlePortClick(event, portId) {
            if (event.target.closest('.port-toggle')) return;
            openModal(portId);
        }

        function updateSettingsUI(settings) {
            const grid = document.getElementById('settingsGrid');
            if (Object.keys(lastSettings).length === 0) {
                let html = '';
                SETTINGS_CONFIG.forEach(s => {
                    const val = settings[String(s.piid)] ?? s.options[0].value;
                    html += `<div class="setting-item"><span class="setting-label">${s.name}</span><select class="setting-select" onchange="setSetting(${s.piid}, parseInt(this.value))">${s.options.map(o => `<option value="${o.value}" ${o.value === val ? 'selected' : ''}>${o.label}</option>`).join('')}</select></div>`;
                });
                grid.innerHTML = html;
            } else {
                SETTINGS_CONFIG.forEach(s => {
                    const select = grid.querySelector(`select[onchange*="${s.piid}"]`);
                    if (select) { const newVal = settings[String(s.piid)] ?? s.options[0].value; if (select.value != newVal) select.value = newVal; }
                });
            }
            lastSettings = settings;
        }

        async function togglePort(port, on) {
            const toggle = document.getElementById(`toggle-${port}`);
            if (toggle) toggle.disabled = true;
            try {
                const res = await fetch(`${API_BASE}/api/port`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ port, action: on ? 'on' : 'off' }) });
                const result = await res.json();
                if (!result.ok) {
                    if (toggle) toggle.checked = !on;
                }
            } catch (e) {
                console.error('Port toggle error:', e);
                if (toggle) toggle.checked = !on;
            } finally {
                if (toggle) toggle.disabled = false;
                fetchStatus();
            }
        }

        async function setSetting(piid, value) {
            try { await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid, value }) }); } catch (e) { console.error('Set setting error:', e); }
        }

        let countdownRendered = false;

        function renderCountdown(settings) {
            const grid = document.getElementById('countdownGrid');
            if (!countdownRendered) {
                let html = '';
                for (const [id, name] of Object.entries(PORT_MAP)) {
                    const key = PORT_KEY_MAP[id];
                    html += `
                        <div class="countdown-item">
                            <div class="countdown-header">
                                <span class="countdown-port ${key}">${name}</span>
                                <span class="countdown-current" id="countdown-status-${key}">未设置</span>
                            </div>
                            <div class="countdown-input-group">
                                <input type="number" class="countdown-input" id="countdown-${key}" min="0" max="1440" placeholder="分钟">
                                <span class="countdown-unit">分钟</span>
                            </div>
                            <div class="countdown-quick">
                                ${QUICK_MINUTES.map(m => `<button class="countdown-quick-btn" onclick="setCountdown('${key}', ${m})">${m}分</button>`).join('')}
                            </div>
                            <div class="countdown-actions">
                                <button class="countdown-toggle-btn set" id="countdown-btn-${key}" onclick="handleCountdownAction('${key}')">设置</button>
                            </div>
                        </div>`;
                }
                grid.innerHTML = html;
                countdownRendered = true;
            }
            for (const [id, name] of Object.entries(PORT_MAP)) {
                const piid = COUNTDOWN_PIIDS[id];
                const currentVal = settings[String(piid)] || 0;
                const key = PORT_KEY_MAP[id];
                const statusEl = document.getElementById(`countdown-status-${key}`);
                if (statusEl) {
                    statusEl.textContent = currentVal > 0 ? currentVal + '分钟' : '未设置';
                }
                const btn = document.getElementById(`countdown-btn-${key}`);
                if (btn && !btn.disabled) {
                    if (currentVal > 0) {
                        btn.textContent = '清除';
                        btn.className = 'countdown-toggle-btn clear';
                    } else {
                        btn.textContent = '设置';
                        btn.className = 'countdown-toggle-btn set';
                    }
                }
            }
        }

        const countdownPending = {};

        async function setCountdown(port, minutes) {
            if (countdownPending[port]) return;
            countdownPending[port] = true;
            const id = PORT_KEY_TO_ID[port];
            const btn = document.getElementById(`countdown-btn-${port}`);
            const statusEl = document.getElementById(`countdown-status-${port}`);
            const isClear = minutes === 0;
            if (btn) { btn.disabled = true; btn.textContent = isClear ? '清除中...' : '设置中...'; }
            const expectedText = minutes > 0 ? minutes + '分钟' : '未设置';
            const revertBtn = () => {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = isClear ? '清除' : '设置';
                    btn.className = `countdown-toggle-btn ${isClear ? 'clear' : 'set'}`;
                }
                if (!isClear) {
                    const input = document.getElementById(`countdown-${port}`);
                    if (input) input.value = '';
                }
            };
            const piid = COUNTDOWN_PIIDS[id];
            if (!piid) { countdownPending[port] = false; revertBtn(); return; }
            try {
                await fetch(`${API_BASE}/api/set`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ piid, value: minutes }) });
                fetchStatus();
                if (statusEl) {
                    const observer = new MutationObserver(() => {
                        if (statusEl.textContent === expectedText) {
                            observer.disconnect();
                            countdownPending[port] = false;
                            revertBtn();
                        }
                    });
                    observer.observe(statusEl, { childList: true, characterData: true, subtree: true });
                    setTimeout(() => { observer.disconnect(); countdownPending[port] = false; revertBtn(); }, 10000);
                } else {
                    setTimeout(() => { countdownPending[port] = false; revertBtn(); }, 3000);
                }
            } catch (e) { console.error('Set countdown error:', e); countdownPending[port] = false; revertBtn(); }
        }

        function setCountdownFromInput(port) {
            const input = document.getElementById(`countdown-${port}`);
            const minutes = parseInt(input.value) || 0;
            setCountdown(port, minutes);
        }

        function handleCountdownAction(port) {
            const btn = document.getElementById(`countdown-btn-${port}`);
            if (btn && btn.classList.contains('clear')) {
                setCountdown(port, 0);
            } else {
                const input = document.getElementById(`countdown-${port}`);
                if (!input.value || parseInt(input.value) <= 0) return;
                setCountdownFromInput(port);
            }
        }

        async function bleToggle() {
            const btn = document.getElementById('bleToggle');
            if (btn.disabled) return;
            btn.disabled = true;
            try {
                const enable = btn.textContent === '连接设备';
                await fetch(`${API_BASE}/api/enable`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: enable }) });
                await new Promise(r => setTimeout(r, 500));
                await fetchStatus();
            } catch (e) { console.error('BLE toggle error:', e); }
            finally { btn.disabled = false; }
        }

        async function bleRestart() {
            const btn = document.getElementById('bleToggle');
            if (btn.disabled) return;
            btn.disabled = true;
            try {
                await fetch(`${API_BASE}/api/enable`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }) });
                await new Promise(r => setTimeout(r, 2000));
                await fetch(`${API_BASE}/api/enable`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: true }) });
                await new Promise(r => setTimeout(r, 500));
                await fetchStatus();
            } catch (e) { console.error('BLE restart error:', e); }
            finally { btn.disabled = false; }
        }

        // Set initial active button
        document.querySelectorAll('.time-btn').forEach(btn => {
            const minutes = btn.textContent === '24小时' ? 1440 : parseInt(btn.textContent);
            btn.classList.toggle('active', minutes === parseInt(localStorage.getItem('cuktech-chart-hours') || '60'));
        });

        function initApp() {
            try {
                initChart();
                fetchChartData();
                pollStatus();
            } catch (e) {
                console.error('Init error:', e);
                pollStatus();
            }
        }

        async function pollStatus() {
            await fetchStatus();
            setTimeout(pollStatus, 2000);
        }

        // Initialize if Chart.js is ready, otherwise wait for CDN fallback
        if (typeof Chart !== 'undefined') {
            initApp();
        } else {
            window.onChartReady = initApp;
        }
