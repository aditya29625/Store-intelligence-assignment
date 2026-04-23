/* dashboard.js — Store Intelligence real-time dashboard */
'use strict';

const API_BASE = window.API_URL || 'http://localhost:8000';
const WS_URL = API_BASE.replace(/^http/, 'ws') + '/ws';
const POLL_INTERVAL = 8000;  // ms between metric refreshes
const MAX_FEED_ITEMS = 60;

// ── State ────────────────────────────────────────────────────────────────────
let currentStore = 'STORE_BLR_002';
let totalEvents = 0;
let ws = null;
let visitorChart = null;
let chartHours = [];
let chartEntries = [];
let chartExits = [];

// ── Clock ────────────────────────────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString('en-IN', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// ── Store selector ───────────────────────────────────────────────────────────
document.getElementById('store-select').addEventListener('change', (e) => {
  currentStore = e.target.value;
  clearFeed();
  refreshAll();
});

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
  if (ws) { try { ws.close(); } catch {} }
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setLive(true);
    // Keep-alive ping
    const ping = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send('ping');
      } else {
        clearInterval(ping);
      }
    }, 15000);
  };

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'event' && msg.data.store_id === currentStore) {
        handleLiveEvent(msg.data);
      }
    } catch {}
  };

  ws.onclose = () => {
    setLive(false);
    // Reconnect after 3s
    setTimeout(connectWS, 3000);
  };

  ws.onerror = () => { setLive(false); };
}

function setLive(connected) {
  const badge = document.getElementById('live-badge');
  const label = document.getElementById('live-label');
  if (connected) {
    badge.classList.remove('disconnected');
    label.textContent = 'LIVE';
  } else {
    badge.classList.add('disconnected');
    label.textContent = 'RECONNECTING';
  }
}

// ── Live event handler ────────────────────────────────────────────────────────
function handleLiveEvent(evt) {
  totalEvents++;
  document.getElementById('event-counter').textContent = `${totalEvents.toLocaleString()} events`;

  addFeedItem(evt);

  // Update chart for ENTRY/EXIT events
  if (evt.event_type === 'ENTRY') {
    const hour = new Date().getHours();
    updateChartHour(hour, 'entry');
  } else if (evt.event_type === 'EXIT') {
    const hour = new Date().getHours();
    updateChartHour(hour, 'exit');
  }
}

// ── Feed ──────────────────────────────────────────────────────────────────────
function addFeedItem(evt) {
  const feed = document.getElementById('event-feed');
  const empty = feed.querySelector('.feed-empty');
  if (empty) empty.remove();

  const cls = (() => {
    if (['ENTRY', 'REENTRY'].includes(evt.event_type)) return 'entry';
    if (evt.event_type === 'EXIT') return 'exit';
    if (evt.event_type.startsWith('BILLING')) return 'billing';
    return '';
  })();

  const item = document.createElement('div');
  item.className = `feed-item ${cls}`;
  item.innerHTML = `
    <div class="feed-type">${evt.event_type.replace(/_/g,' ')}</div>
    <div class="feed-details">
      <div class="feed-visitor">${evt.visitor_id}${evt.is_staff ? ' 👔 Staff' : ''}</div>
      <div class="feed-meta">
        ${evt.zone_id ? `Zone: ${evt.zone_id} · ` : ''}
        Confidence: ${(evt.confidence*100).toFixed(0)}%
      </div>
    </div>
  `;

  feed.insertBefore(item, feed.firstChild);

  // Trim old items
  const items = feed.querySelectorAll('.feed-item');
  if (items.length > MAX_FEED_ITEMS) {
    items[items.length - 1].remove();
  }

  document.getElementById('feed-count').textContent = Math.min(totalEvents, MAX_FEED_ITEMS);
}

function clearFeed() {
  const feed = document.getElementById('event-feed');
  feed.innerHTML = '<div class="feed-empty">Switched store — waiting for events...</div>';
  totalEvents = 0;
  document.getElementById('event-counter').textContent = '0 events';
  document.getElementById('feed-count').textContent = '0';
}

// ── API fetch helpers ─────────────────────────────────────────────────────────
async function apiFetch(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ── Metrics ───────────────────────────────────────────────────────────────────
async function refreshMetrics() {
  try {
    const data = await apiFetch(`/stores/${currentStore}/metrics`);
    document.getElementById('val-visitors').textContent = data.unique_visitors;
    document.getElementById('val-conversion').textContent =
      (data.conversion_rate * 100).toFixed(1) + '%';
    document.getElementById('val-queue').textContent = data.queue_depth;
    document.getElementById('val-abandon').textContent =
      (data.abandonment_rate * 100).toFixed(1) + '%';

    // Colour KPI cards based on thresholds
    const queueCard = document.getElementById('kpi-queue');
    queueCard.classList.toggle('alert-critical', data.queue_depth >= 5);
    queueCard.classList.toggle('alert-warn', data.queue_depth >= 3 && data.queue_depth < 5);
  } catch (e) {
    console.warn('Metrics fetch failed:', e);
  }
}

// ── Funnel ────────────────────────────────────────────────────────────────────
async function refreshFunnel() {
  try {
    const data = await apiFetch(`/stores/${currentStore}/funnel`);
    const stages = Object.fromEntries(data.stages.map(s => [s.stage, s]));
    const maxCount = data.stages[0]?.count || 1;

    const stageMap = {
      'ENTRY': 'entry',
      'ZONE_VISIT': 'zone',
      'BILLING_QUEUE': 'billing',
      'PURCHASE': 'purchase',
    };

    for (const [apiKey, domKey] of Object.entries(stageMap)) {
      const s = stages[apiKey];
      if (!s) continue;
      document.getElementById(`funnel-count-${domKey}`).textContent = s.count;
      const pct = Math.round((s.count / maxCount) * 100);
      document.getElementById(`funnel-bar-${domKey}`).style.width = pct + '%';
      if (domKey !== 'entry' && domKey !== 'purchase') {
        const dropEl = document.getElementById(`funnel-drop-${domKey}`);
        if (dropEl) {
          dropEl.textContent = s.drop_off_pct > 0 ? `↓ ${s.drop_off_pct}% drop-off` : '';
        }
      }
    }
  } catch (e) {
    console.warn('Funnel fetch failed:', e);
  }
}

// ── Heatmap ───────────────────────────────────────────────────────────────────
const HEATMAP_COLORS = [
  [16, 185, 129],   // green (cold)
  [6, 182, 212],    // cyan
  [99, 102, 241],   // indigo
  [139, 92, 246],   // violet
  [236, 72, 153],   // pink (hot)
];

function scoreToColor(score) {
  const idx = (score / 100) * (HEATMAP_COLORS.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  const t = idx - lo;
  const c = HEATMAP_COLORS[lo];
  const d = HEATMAP_COLORS[Math.min(hi, HEATMAP_COLORS.length - 1)];
  const r = Math.round(c[0] + t * (d[0] - c[0]));
  const g = Math.round(c[1] + t * (d[1] - c[1]));
  const b = Math.round(c[2] + t * (d[2] - c[2]));
  return `rgba(${r},${g},${b},0.75)`;
}

async function refreshHeatmap() {
  try {
    const data = await apiFetch(`/stores/${currentStore}/heatmap`);
    const grid = document.getElementById('heatmap-grid');
    grid.innerHTML = '';

    document.getElementById('heatmap-confidence').textContent =
      data.data_confidence ? 'High confidence' : 'Low data (<20 sessions)';

    if (!data.zones.length) {
      grid.innerHTML = '<div class="feed-empty">No zone data yet</div>';
      return;
    }

    for (const zone of data.zones) {
      const dwell = zone.avg_dwell_ms > 0
        ? (zone.avg_dwell_ms / 1000).toFixed(0) + 's avg dwell'
        : 'No dwell data';
      const el = document.createElement('div');
      el.className = 'heatmap-zone';
      el.style.background = scoreToColor(zone.normalised_score);
      el.innerHTML = `
        <div class="heatmap-zone-name">${zone.zone_id}</div>
        <div class="heatmap-zone-stat">${zone.visit_count} visits · ${dwell}</div>
        <div class="heatmap-score">${zone.normalised_score.toFixed(0)}</div>
      `;
      grid.appendChild(el);
    }
  } catch (e) {
    console.warn('Heatmap fetch failed:', e);
  }
}

// ── Anomalies ─────────────────────────────────────────────────────────────────
async function refreshAnomalies() {
  try {
    const data = await apiFetch(`/stores/${currentStore}/anomalies`);
    const list = document.getElementById('anomaly-list');
    const count = data.active_anomalies.length;
    document.getElementById('anomaly-count').textContent = count;

    if (!count) {
      list.innerHTML = '<div class="feed-empty">All systems nominal ✓</div>';
      return;
    }

    list.innerHTML = '';
    for (const a of data.active_anomalies) {
      const el = document.createElement('div');
      el.className = `anomaly-item ${a.severity}`;
      el.innerHTML = `
        <div class="anomaly-type">${a.severity} · ${a.anomaly_type.replace(/_/g,' ')}</div>
        <div class="anomaly-desc">${a.description}</div>
        <div class="anomaly-action">→ ${a.suggested_action}</div>
      `;
      list.appendChild(el);
    }
  } catch (e) {
    console.warn('Anomaly fetch failed:', e);
  }
}

// ── Health ────────────────────────────────────────────────────────────────────
async function refreshHealth() {
  try {
    const data = await apiFetch('/health');
    const list = document.getElementById('health-list');
    list.innerHTML = '';

    for (const store of data.stores) {
      const el = document.createElement('div');
      el.className = 'health-item';
      const statusClass = store.status === 'OK' ? 'ok' : store.status === 'STALE_FEED' ? 'stale' : 'nodata';
      el.innerHTML = `
        <span class="health-store">${store.store_id}</span>
        <span class="health-lag">${store.lag_minutes != null ? store.lag_minutes.toFixed(0) + ' min ago' : 'No data'}</span>
        <span class="health-status ${statusClass}">${store.status}</span>
      `;
      list.appendChild(el);
    }
  } catch (e) {
    console.warn('Health fetch failed:', e);
  }
}

// ── Visitor Chart ─────────────────────────────────────────────────────────────
function initChart() {
  const now = new Date();
  const currentHour = now.getHours();
  chartHours = [];
  chartEntries = [];
  chartExits = [];

  for (let h = Math.max(0, currentHour - 5); h <= currentHour; h++) {
    chartHours.push(`${h}:00`);
    chartEntries.push(0);
    chartExits.push(0);
  }

  const ctx = document.getElementById('visitor-chart').getContext('2d');
  visitorChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: chartHours,
      datasets: [
        {
          label: 'Entries',
          data: chartEntries,
          backgroundColor: 'rgba(16,185,129,0.6)',
          borderColor: 'rgba(16,185,129,1)',
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          label: 'Exits',
          data: chartExits,
          backgroundColor: 'rgba(239,68,68,0.6)',
          borderColor: 'rgba(239,68,68,1)',
          borderWidth: 1,
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: {
          ticks: { color: '#64748b', font: { size: 10 } },
          grid: { color: 'rgba(100,116,139,0.1)' },
        },
        y: {
          beginAtZero: true,
          ticks: { color: '#64748b', font: { size: 10 }, stepSize: 1 },
          grid: { color: 'rgba(100,116,139,0.1)' },
        },
      },
    },
  });
}

function updateChartHour(hour, type) {
  const label = `${hour}:00`;
  let idx = chartHours.indexOf(label);
  if (idx === -1) {
    chartHours.push(label);
    chartEntries.push(0);
    chartExits.push(0);
    idx = chartHours.length - 1;
    // Keep only last 6 hours
    if (chartHours.length > 6) {
      chartHours.shift();
      chartEntries.shift();
      chartExits.shift();
      idx--;
    }
  }
  if (type === 'entry') { chartEntries[idx]++; }
  else { chartExits[idx]++; }

  if (visitorChart) {
    visitorChart.data.labels = [...chartHours];
    visitorChart.data.datasets[0].data = [...chartEntries];
    visitorChart.data.datasets[1].data = [...chartExits];
    visitorChart.update('none');
  }
}

// ── Synthetic demo replay ─────────────────────────────────────────────────────
async function startDemoReplay() {
  // Seed the API with synthetic events for all stores
  const stores = ['STORE_BLR_001','STORE_BLR_002','STORE_MUM_001','STORE_DEL_001','STORE_HYD_001'];
  const eventTypes = ['ENTRY','EXIT','ZONE_ENTER','ZONE_EXIT','ZONE_DWELL','BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON','REENTRY'];
  const zones = ['SKINCARE','HAIRCARE','MAKEUP','WELLNESS','FRAGRANCE','BILLING','BILLING_QUEUE'];

  function makeEvent(storeId) {
    const now = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
    const et = eventTypes[Math.floor(Math.random() * 6)];
    return {
      event_id: crypto.randomUUID(),
      store_id: storeId,
      camera_id: 'CAM_ENTRY_01',
      visitor_id: 'VIS_' + Math.random().toString(36).substr(2,6),
      event_type: et,
      timestamp: now,
      zone_id: ['ZONE_ENTER','ZONE_EXIT','ZONE_DWELL','BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON'].includes(et)
        ? zones[Math.floor(Math.random() * zones.length)] : null,
      dwell_ms: Math.floor(Math.random() * 120000),
      is_staff: Math.random() < 0.1,
      confidence: 0.75 + Math.random() * 0.25,
      metadata: {
        queue_depth: et === 'BILLING_QUEUE_JOIN' ? Math.floor(Math.random()*5)+1 : null,
        sku_zone: null,
        session_seq: Math.floor(Math.random()*8)+1,
      }
    };
  }

  // Generate a batch for each store and POST
  for (const sid of stores) {
    const events = Array.from({length: 12}, () => makeEvent(sid));
    try {
      await fetch(`${API_BASE}/events/ingest`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({events}),
      });
    } catch {}
  }
}

// ── Refresh all ───────────────────────────────────────────────────────────────
async function refreshAll() {
  await Promise.allSettled([
    refreshMetrics(),
    refreshFunnel(),
    refreshHeatmap(),
    refreshAnomalies(),
    refreshHealth(),
  ]);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initChart();
  connectWS();

  // Seed demo data on load
  await startDemoReplay();

  await refreshAll();

  // Poll periodically
  setInterval(async () => {
    await startDemoReplay();  // Keep demo data flowing
    await refreshAll();
  }, POLL_INTERVAL);
});
