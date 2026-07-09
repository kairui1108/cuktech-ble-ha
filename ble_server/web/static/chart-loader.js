// Chart.js loader with CDN fallback
// Runs synchronously in <head>, no IIFE needed
if (typeof Chart === 'undefined') {
    var localScript = document.querySelector('script[src="/static/chart.umd.min.js"]');
    if (localScript) {
        localScript.onerror = function() {
            console.warn('Local Chart.js failed, loading from CDN...');
            var s = document.createElement('script');
            s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js';
            s.onload = function() {
                if (typeof window.onChartReady === 'function') {
                    window.onChartReady();
                }
            };
            s.onerror = function() {
                console.error('Failed to load Chart.js from CDN');
            };
            document.head.appendChild(s);
        };
    }
}
