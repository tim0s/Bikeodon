(function () {
  "use strict";

  const WHEEL_CIRCUMFERENCE_MM = 2105; // 700x25c default

  const state = {
    trainer: { device: null, server: null },
    hr: { device: null, server: null },
    csc: { device: null, server: null, prevWheelRevs: null, prevWheelTime: null, prevCrankRevs: null, prevCrankTime: null },
    latest: { power: null, cadence: null, hr: null, speed: null },
    samples: [],
    startTime: null,
    chart: null,
    updateTimer: null,
    workout: null,
  };

  function $(id) { return document.getElementById(id); }

  function setStatus(role, stateName, label) {
    const el = $(`status-${role}`);
    if (!el) return;
    el.dataset.state = stateName;
    el.textContent = label;
  }

  function anyConnected() {
    return !!(state.trainer.server || state.hr.server || state.csc.server);
  }

  function refreshStartButton() {
    $("start-btn").disabled = !anyConnected();
  }

  // ---- Smart trainer (FTMS) -------------------------------------------------

  function parseIndoorBikeData(dataView) {
    let offset = 0;
    const flags = dataView.getUint16(offset, true); offset += 2;
    const result = {};

    // Bit 0: More Data (instantaneous speed present unless set) — bit 0 = 0 means speed IS present
    if ((flags & 0x0001) === 0) {
      result.speed = dataView.getUint16(offset, true) / 100; // km/h
      offset += 2;
    }
    if (flags & 0x0002) { offset += 2; } // avg speed, skip
    if (flags & 0x0004) {
      result.cadence = dataView.getUint16(offset, true) / 2; // rpm
      offset += 2;
    }
    if (flags & 0x0008) { offset += 2; } // avg cadence, skip
    if (flags & 0x0010) { offset += 3; } // total distance (24-bit), skip
    if (flags & 0x0020) { offset += 2; } // resistance level, skip
    if (flags & 0x0040) {
      result.power = dataView.getInt16(offset, true); // watts
      offset += 2;
    }
    // remaining fields (avg power, expended energy, heart rate, metabolic
    // equivalent, elapsed time, remaining time) are not needed here.
    return result;
  }

  async function connectTrainer() {
    if (!navigator.bluetooth) return;
    setStatus("trainer", "scanning", "Scanning…");
    try {
      const device = await navigator.bluetooth.requestDevice({
        filters: [{ services: ["fitness_machine"] }],
      });
      state.trainer.device = device;
      device.addEventListener("gattserverdisconnected", () => {
        state.trainer.server = null;
        setStatus("trainer", "disconnected", "Disconnected");
        refreshStartButton();
      });
      const server = await device.gatt.connect();
      const service = await server.getPrimaryService("fitness_machine");
      const characteristic = await service.getCharacteristic("indoor_bike_data");
      await characteristic.startNotifications();
      characteristic.addEventListener("characteristicvaluechanged", (ev) => {
        const parsed = parseIndoorBikeData(ev.target.value);
        if (parsed.power !== undefined) state.latest.power = parsed.power;
        if (parsed.cadence !== undefined) state.latest.cadence = parsed.cadence;
        if (parsed.speed !== undefined) state.latest.speed = parsed.speed;
      });
      state.trainer.server = server;
      setStatus("trainer", "connected", device.name || "Smart trainer");
    } catch (err) {
      console.error("Trainer connect failed", err);
      setStatus("trainer", "error", "Connection failed");
    }
    refreshStartButton();
  }

  // ---- Heart rate -------------------------------------------------------

  function parseHeartRate(dataView) {
    const flags = dataView.getUint8(0);
    const is16bit = flags & 0x01;
    const bpm = is16bit ? dataView.getUint16(1, true) : dataView.getUint8(1);
    return bpm;
  }

  async function connectHeartRate() {
    if (!navigator.bluetooth) return;
    setStatus("hr", "scanning", "Scanning…");
    try {
      const device = await navigator.bluetooth.requestDevice({
        filters: [{ services: ["heart_rate"] }],
      });
      state.hr.device = device;
      device.addEventListener("gattserverdisconnected", () => {
        state.hr.server = null;
        setStatus("hr", "disconnected", "Disconnected");
        refreshStartButton();
      });
      const server = await device.gatt.connect();
      const service = await server.getPrimaryService("heart_rate");
      const characteristic = await service.getCharacteristic("heart_rate_measurement");
      await characteristic.startNotifications();
      characteristic.addEventListener("characteristicvaluechanged", (ev) => {
        state.latest.hr = parseHeartRate(ev.target.value);
      });
      state.hr.server = server;
      setStatus("hr", "connected", device.name || "Heart rate monitor");
    } catch (err) {
      console.error("Heart rate connect failed", err);
      setStatus("hr", "error", "Connection failed");
    }
    refreshStartButton();
  }

  // ---- Speed & cadence ----------------------------------------------------

  function parseCscMeasurement(dataView, prev) {
    let offset = 0;
    const flags = dataView.getUint8(offset); offset += 1;
    const result = {};

    if (flags & 0x01) {
      const wheelRevs = dataView.getUint32(offset, true); offset += 4;
      const wheelTime = dataView.getUint16(offset, true); offset += 2; // 1/1024s
      if (prev.prevWheelRevs !== null) {
        let dRevs = wheelRevs - prev.prevWheelRevs;
        let dTime = wheelTime - prev.prevWheelTime;
        if (dTime < 0) dTime += 65536; // rollover
        if (dRevs < 0) dRevs += 4294967296;
        if (dTime > 0) {
          const seconds = dTime / 1024;
          const distanceMm = dRevs * WHEEL_CIRCUMFERENCE_MM;
          result.speed = (distanceMm / 1e6) / (seconds / 3600); // km/h
        }
      }
      prev.prevWheelRevs = wheelRevs;
      prev.prevWheelTime = wheelTime;
    }

    if (flags & 0x02) {
      const crankRevs = dataView.getUint16(offset, true); offset += 2;
      const crankTime = dataView.getUint16(offset, true); offset += 2; // 1/1024s
      if (prev.prevCrankRevs !== null) {
        let dRevs = crankRevs - prev.prevCrankRevs;
        let dTime = crankTime - prev.prevCrankTime;
        if (dTime < 0) dTime += 65536;
        if (dRevs < 0) dRevs += 65536;
        if (dTime > 0) {
          result.cadence = (dRevs / (dTime / 1024)) * 60; // rpm
        }
      }
      prev.prevCrankRevs = crankRevs;
      prev.prevCrankTime = crankTime;
    }

    return result;
  }

  async function connectCsc() {
    if (!navigator.bluetooth) return;
    setStatus("csc", "scanning", "Scanning…");
    try {
      const device = await navigator.bluetooth.requestDevice({
        filters: [{ services: ["cycling_speed_and_cadence"] }],
      });
      state.csc.device = device;
      device.addEventListener("gattserverdisconnected", () => {
        state.csc.server = null;
        setStatus("csc", "disconnected", "Disconnected");
        refreshStartButton();
      });
      const server = await device.gatt.connect();
      const service = await server.getPrimaryService("cycling_speed_and_cadence");
      const characteristic = await service.getCharacteristic("csc_measurement");
      await characteristic.startNotifications();
      characteristic.addEventListener("characteristicvaluechanged", (ev) => {
        const parsed = parseCscMeasurement(ev.target.value, state.csc);
        if (parsed.cadence !== undefined) state.latest.cadence = parsed.cadence;
        if (parsed.speed !== undefined) state.latest.speed = parsed.speed;
      });
      state.csc.server = server;
      setStatus("csc", "connected", device.name || "Speed/cadence sensor");
    } catch (err) {
      console.error("Speed/cadence connect failed", err);
      setStatus("csc", "error", "Connection failed");
    }
    refreshStartButton();
  }

  // ---- Workout builder ------------------------------------------------------

  function hardnessLabel(pct) {
    if (pct <= 20) return "Easy";
    if (pct <= 40) return "Moderate-easy";
    if (pct <= 60) return "Moderate";
    if (pct <= 80) return "Hard";
    return "Very hard";
  }

  function fmtDuration(totalSeconds) {
    const m = Math.round(totalSeconds / 60);
    return m + " min";
  }

  function renderWorkoutError(message) {
    const el = $("workout-error");
    el.textContent = message;
    el.hidden = false;
    $("workout-preview").hidden = true;
  }

  function renderWorkoutPreview(result) {
    $("workout-error").hidden = true;
    const totalS = result.steps.reduce((sum, s) => sum + s.duration_s, 0);
    $("preview-duration").textContent = fmtDuration(totalS);
    $("preview-np").textContent = result.planned_np !== null ? result.planned_np + " W" : "--";
    $("preview-if").textContent = result.planned_if !== null ? result.planned_if.toFixed(2) : "--";
    $("preview-tss").textContent = result.planned_tss !== null ? result.planned_tss : "--";

    const tbody = $("preview-steps");
    tbody.innerHTML = "";
    result.steps.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${s.label}</td><td>${fmtDuration(s.duration_s)}</td><td>${s.pct_ftp}%</td><td>${s.watts} W</td>`;
      tbody.appendChild(tr);
    });

    $("workout-preview").hidden = false;
    state.workout = result;
  }

  async function generateWorkout() {
    const goal = $("goal-select").value;
    const durationMin = parseInt($("duration-range").value, 10);
    const hardness = parseInt($("hardness-range").value, 10) / 100;

    try {
      const resp = await fetch("/training/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, duration_min: durationMin, hardness }),
      });
      const result = await resp.json();
      if (!result.ok) {
        renderWorkoutError(result.message || "Couldn't generate that workout.");
        return;
      }
      renderWorkoutPreview(result);
    } catch (err) {
      console.error("Workout generation failed", err);
      renderWorkoutError("Something went wrong generating the workout.");
    }
  }

  function showPairingView() {
    $("builder-view").hidden = true;
    $("pairing-view").hidden = false;
  }

  async function downloadWorkout(kind) {
    if (!state.workout) return;
    try {
      const resp = await fetch(`/training/export.${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.workout),
      });
      if (!resp.ok) throw new Error(`export failed: ${resp.status}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const disposition = resp.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const a = document.createElement("a");
      a.href = url;
      a.download = match ? match[1] : `workout.${kind}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Workout download failed", err);
    }
  }

  // ---- Recording / chart ---------------------------------------------------

  function fmt(value, digits) {
    return value === null || value === undefined ? "--" : value.toFixed(digits);
  }

  function updateStatChips() {
    $("chip-power").textContent = fmt(state.latest.power, 0);
    $("chip-cadence").textContent = fmt(state.latest.cadence, 0);
    $("chip-hr").textContent = fmt(state.latest.hr, 0);
    $("chip-speed").textContent = fmt(state.latest.speed, 1);
  }

  function tick() {
    const elapsed = (Date.now() - state.startTime) / 1000;
    state.samples.push({
      t: elapsed,
      power: state.latest.power,
      cadence: state.latest.cadence,
      hr: state.latest.hr,
      speed: state.latest.speed,
    });
    updateStatChips();

    const chart = state.chart;
    const label = elapsed.toFixed(0) + "s";
    chart.data.labels.push(label);
    chart.data.datasets[0].data.push(state.latest.power);
    chart.data.datasets[1].data.push(state.latest.hr);
    chart.data.datasets[2].data.push(state.latest.cadence);
    chart.data.datasets[3].data.push(state.latest.speed);

    const maxPoints = 300; // 5 minutes at 1Hz
    if (chart.data.labels.length > maxPoints) {
      chart.data.labels.shift();
      chart.data.datasets.forEach((ds) => ds.data.shift());
    }
    chart.update("none");
  }

  function initChart() {
    const ctx = $("trainingChart").getContext("2d");
    state.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { label: "Power (W)", data: [], borderColor: "#FC4C02", backgroundColor: "transparent", yAxisID: "y", tension: 0.2, pointRadius: 0 },
          { label: "Heart rate (bpm)", data: [], borderColor: "#d32f2f", backgroundColor: "transparent", yAxisID: "y1", tension: 0.2, pointRadius: 0 },
          { label: "Cadence (rpm)", data: [], borderColor: "#2e8b47", backgroundColor: "transparent", yAxisID: "y1", tension: 0.2, pointRadius: 0 },
          { label: "Speed (km/h)", data: [], borderColor: "#3b6fd6", backgroundColor: "transparent", yAxisID: "y1", tension: 0.2, pointRadius: 0 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: {
          x: { ticks: { maxTicksLimit: 8 } },
          y: { position: "left", title: { display: true, text: "Watts" }, beginAtZero: true },
          y1: { position: "right", title: { display: true, text: "bpm / rpm / km/h" }, beginAtZero: true, grid: { drawOnChartArea: false } },
        },
      },
    });
  }

  function startSession() {
    $("pairing-view").hidden = true;
    $("live-view").hidden = false;
    state.startTime = Date.now();
    state.samples = [];
    initChart();
    state.updateTimer = setInterval(tick, 1000);
  }

  function disconnectAll() {
    [state.trainer, state.hr, state.csc].forEach((entry) => {
      if (entry.device && entry.device.gatt && entry.device.gatt.connected) {
        entry.device.gatt.disconnect();
      }
      entry.server = null;
    });
    setStatus("trainer", "disconnected", "Disconnected");
    setStatus("hr", "disconnected", "Disconnected");
    setStatus("csc", "disconnected", "Disconnected");
  }

  function stopSession() {
    clearInterval(state.updateTimer);
    state.updateTimer = null;
    disconnectAll();
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    $("live-view").hidden = true;
    $("builder-view").hidden = false;
    $("pairing-view").hidden = true;
    refreshStartButton();
  }

  function init() {
    const bluetoothSupported = !!navigator.bluetooth;
    if (!bluetoothSupported) {
      $("bluetooth-warning").hidden = false;
    }

    // Workout generation/export never needs Bluetooth, so this is wired up regardless.
    $("duration-range").addEventListener("input", (e) => {
      $("duration-label").textContent = e.target.value;
    });
    $("hardness-range").addEventListener("input", (e) => {
      $("hardness-label").textContent = hardnessLabel(parseInt(e.target.value, 10));
    });
    $("generate-btn").addEventListener("click", generateWorkout);
    $("skip-builder-link").addEventListener("click", (e) => { e.preventDefault(); showPairingView(); });
    $("start-workout-btn").addEventListener("click", showPairingView);
    $("download-fit-btn").addEventListener("click", () => downloadWorkout("fit"));
    $("download-zwo-btn").addEventListener("click", () => downloadWorkout("zwo"));

    if (bluetoothSupported) {
      $("connect-trainer-btn").addEventListener("click", connectTrainer);
      $("connect-hr-btn").addEventListener("click", connectHeartRate);
      $("connect-csc-btn").addEventListener("click", connectCsc);
      $("start-btn").addEventListener("click", startSession);
      $("stop-btn").addEventListener("click", stopSession);
    } else {
      ["connect-trainer-btn", "connect-hr-btn", "connect-csc-btn"].forEach((id) => {
        $(id).disabled = true;
      });
    }
    refreshStartButton();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
