// Shared chart configuration and utilities
const HISTORY_INTERVALS = { 30: 20, 60: 20, 90: 20, 120: 30, 1440: 300 };
const MAX_POINTS_MAP = { 30: 90, 60: 180, 90: 270, 120: 240, 1440: 288 };
let _currentHours = parseInt(localStorage.getItem('cuktech-chart-hours') || '60') / 60;

function getMaxPoints() {
    const minutes = Math.round(_currentHours * 60);
    return MAX_POINTS_MAP[minutes] || 120;
}

function getInterval() {
    const minutes = Math.round(_currentHours * 60);
    return HISTORY_INTERVALS[minutes] || 20;
}

function setCurrentHours(hours) {
    _currentHours = hours;
}

function getCurrentHours() {
    return _currentHours;
}

function getChartColors() {
    const cs = getComputedStyle(document.documentElement);
    return {
        c1: cs.getPropertyValue('--port-c1').trim() || '#03a9f4',
        c2: cs.getPropertyValue('--port-c2').trim() || '#7c4dff',
        c3: cs.getPropertyValue('--port-c3').trim() || '#389e3d',
        a: cs.getPropertyValue('--port-a').trim() || '#ffa42b',
        text: cs.getPropertyValue('--text').trim() || '#e1e1e1',
        textDim: cs.getPropertyValue('--text-dim').trim() || '#959595',
        accent: cs.getPropertyValue('--accent').trim() || '#03a9f4',
    };
}
