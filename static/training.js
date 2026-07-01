(function () {
  "use strict";

  const WHEEL_CIRCUMFERENCE_MM = 2105; // 700x25c default

  const state = {
    trainer: { device: null, server: null, controlChar: null, ergActive: false },
    hr: { device: null, server: null },
    csc: { device: null, server: null, prevWheelRevs: null, prevWheelTime: null, prevCrankRevs: null, prevCrankTime: null },
    latest: { power: null, cadence: null, hr: null, speed: null },
    samples: [],
    startTime: null,
    chart: null,
    updateTimer: null,
    workout: null,
    previewChart: null,
    savedWorkouts: [],
    librarySort: { col: "created_at", dir: "desc" },
    libraryLoaded: false,
    playback: null,
    lastStepIndex: null,
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
        state.trainer.controlChar = null;
        state.trainer.ergActive = false;
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

      try {
        state.trainer.controlChar = await service.getCharacteristic("fitness_machine_control_point");
      } catch (ergErr) {
        state.trainer.controlChar = null; // trainer doesn't expose Control Point — ERG unavailable
      }

      state.trainer.server = server;
      setStatus("trainer", "connected", device.name || "Smart trainer");
    } catch (err) {
      console.error("Trainer connect failed", err);
      setStatus("trainer", "error", "Connection failed");
    }
    refreshStartButton();
  }

  // ---- FTMS Control Point (ERG mode) ----------------------------------
  // Op codes/response format verified against the Bluetooth SIG Fitness
  // Machine Service v1.0 spec (Table 4.15 / 4.16.2.22).

  const FTMS_OP_REQUEST_CONTROL = 0x00;
  const FTMS_OP_SET_TARGET_POWER = 0x05;
  const FTMS_OP_START_RESUME = 0x07;
  const FTMS_OP_STOP_PAUSE = 0x08;
  const FTMS_OP_RESPONSE = 0x80;
  const FTMS_RESULT_SUCCESS = 0x01;

  function waitForIndication(controlChar, opcode, timeoutMs = 4000) {
    return new Promise((resolve, reject) => {
      const handler = (ev) => {
        const dv = ev.target.value;
        if (dv.byteLength >= 3 && dv.getUint8(0) === FTMS_OP_RESPONSE && dv.getUint8(1) === opcode) {
          controlChar.removeEventListener("characteristicvaluechanged", handler);
          clearTimeout(timer);
          resolve(dv.getUint8(2)); // result code
        }
      };
      const timer = setTimeout(() => {
        controlChar.removeEventListener("characteristicvaluechanged", handler);
        reject(new Error(`FTMS control point timeout (op 0x${opcode.toString(16)})`));
      }, timeoutMs);
      controlChar.addEventListener("characteristicvaluechanged", handler);
    });
  }

  async function writeControlPoint(controlChar, bytes) {
    const buf = Uint8Array.from(bytes);
    if (controlChar.writeValueWithResponse) {
      await controlChar.writeValueWithResponse(buf);
    } else {
      await controlChar.writeValue(buf); // older Chrome fallback
    }
  }

  async function enableErgMode() {
    const controlChar = state.trainer.controlChar;
    if (!controlChar) return false;
    try {
      await controlChar.startNotifications(); // enables indications too

      const requestPromise = waitForIndication(controlChar, FTMS_OP_REQUEST_CONTROL);
      await writeControlPoint(controlChar, [FTMS_OP_REQUEST_CONTROL]);
      if ((await requestPromise) !== FTMS_RESULT_SUCCESS) throw new Error("Request Control not granted");

      const startPromise = waitForIndication(controlChar, FTMS_OP_START_RESUME);
      await writeControlPoint(controlChar, [FTMS_OP_START_RESUME]);
      if ((await startPromise) !== FTMS_RESULT_SUCCESS) throw new Error("Start or Resume failed");

      state.trainer.ergActive = true;
      return true;
    } catch (err) {
      console.error("ERG mode setup failed, falling back to non-ERG", err);
      state.trainer.ergActive = false;
      return false;
    }
  }

  async function setErgTarget(watts) {
    if (!state.trainer.ergActive || !state.trainer.controlChar) return;
    try {
      const buf = new ArrayBuffer(3);
      const dv = new DataView(buf);
      dv.setUint8(0, FTMS_OP_SET_TARGET_POWER);
      dv.setInt16(1, Math.round(watts), true);
      await writeControlPoint(state.trainer.controlChar, new Uint8Array(buf));
    } catch (err) {
      console.error("Set Target Power failed", err);
    }
  }

  async function releaseErgControl() {
    if (!state.trainer.ergActive || !state.trainer.controlChar) return;
    try {
      await writeControlPoint(state.trainer.controlChar, [FTMS_OP_STOP_PAUSE, 0x01]); // 0x01 = Stop
    } catch (err) {
      console.error("Releasing ERG control failed", err);
    }
    state.trainer.ergActive = false;
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
    renderWorkoutProfileChart(result);
  }

  function renderWorkoutProfileChart(result) {
    if (state.previewChart) { state.previewChart.destroy(); state.previewChart = null; }

    const points = [];
    let t = 0;
    result.steps.forEach((step, i) => {
      points.push({ x: t / 60, y: step.watts, stepIndex: i });
      t += step.duration_s;
      points.push({ x: t / 60, y: step.watts, stepIndex: i });
    });

    const zoneColor = (ctx) => {
      const idx = ctx.p1.raw.stepIndex;
      return result.steps[idx].zone_color;
    };

    const ctx = $("workoutProfileChart").getContext("2d");
    state.previewChart = new Chart(ctx, {
      type: "line",
      data: {
        datasets: [
          {
            data: points,
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            tension: 0,
            segment: {
              borderColor: (segCtx) => zoneColor(segCtx),
              backgroundColor: (segCtx) => zoneColor(segCtx) + "40",
            },
          },
          {
            // flat reference line at 100% FTP
            data: [{ x: 0, y: result.ftp }, { x: t / 60, y: result.ftp }],
            borderColor: "#888",
            borderDash: [4, 4],
            borderWidth: 1,
            pointRadius: 0,
            fill: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { type: "linear", title: { display: true, text: "Minutes" } },
          y: { title: { display: true, text: "Watts" }, beginAtZero: true },
        },
      },
    });
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

  function updateErgToggleAvailability() {
    const toggle = $("erg-toggle");
    const caption = $("erg-toggle-caption");
    const hasWorkout = !!state.workout;
    toggle.disabled = !hasWorkout;
    caption.textContent = hasWorkout ? "" : "(requires a loaded workout)";
  }

  function showPairingView() {
    $("builder-view").hidden = true;
    $("pairing-view").hidden = false;
    updateErgToggleAvailability();
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

  // ---- Tabs (generate / custom / library) -----------------------------------

  function switchMode(mode) {
    ["generate", "custom", "library"].forEach((m) => {
      $(`${m}-panel`).hidden = m !== mode;
    });
    $("workout-tabs").querySelectorAll("a[data-mode]").forEach((a) => {
      a.setAttribute("aria-selected", a.dataset.mode === mode ? "true" : "false");
    });
    if (mode === "custom" && $("custom-steps").children.length === 0) {
      addCustomStepRow();
    }
    if (mode === "library") {
      loadLibrary();
    }
  }

  // ---- Custom workout builder -------------------------------------------

  function addCustomStepRow() {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" class="step-label" placeholder="Step"></td>
      <td><input type="number" class="step-duration" min="0.1" step="0.5" value="5"></td>
      <td><input type="number" class="step-pct" min="10" max="300" step="1" value="100"></td>
      <td><button type="button" class="secondary outline remove-step-btn">&times;</button></td>
    `;
    tr.querySelector(".remove-step-btn").addEventListener("click", () => tr.remove());
    $("custom-steps").appendChild(tr);
  }

  async function previewCustomWorkout() {
    const rows = Array.from($("custom-steps").querySelectorAll("tr"));
    const steps = rows.map((tr) => ({
      label: tr.querySelector(".step-label").value,
      duration_s: parseFloat(tr.querySelector(".step-duration").value || "0") * 60,
      pct_ftp: parseFloat(tr.querySelector(".step-pct").value || "0"),
    }));
    const goalLabel = $("custom-focus").value;

    try {
      const resp = await fetch("/training/custom/finalize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ steps, goal_label: goalLabel }),
      });
      const result = await resp.json();
      if (!result.ok) {
        renderWorkoutError(result.message || "Couldn't build that workout.");
        return;
      }
      renderWorkoutPreview(result);
    } catch (err) {
      console.error("Custom workout preview failed", err);
      renderWorkoutError("Something went wrong building the workout.");
    }
  }

  // ---- Save workout ----------------------------------------------------

  async function saveWorkout() {
    if (!state.workout) return;
    const name = prompt("Name this workout:");
    if (!name) return;
    const btn = $("save-workout-btn");
    try {
      const resp = await fetch("/training/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, workout: state.workout }),
      });
      const result = await resp.json();
      if (!result.ok) {
        renderWorkoutError(result.message || "Couldn't save that workout.");
        return;
      }
      const original = btn.textContent;
      btn.textContent = "Saved!";
      setTimeout(() => { btn.textContent = original; }, 1500);
    } catch (err) {
      console.error("Save workout failed", err);
      renderWorkoutError("Something went wrong saving the workout.");
    }
  }

  // ---- Saved workout library ---------------------------------------------

  async function loadLibrary() {
    try {
      const resp = await fetch("/training/saved");
      const result = await resp.json();
      state.savedWorkouts = result.ok ? result.workouts : [];
    } catch (err) {
      console.error("Loading saved workouts failed", err);
      state.savedWorkouts = [];
    }
    renderLibrary();
  }

  function sortLibrary(col) {
    if (state.librarySort.col === col) {
      state.librarySort.dir = state.librarySort.dir === "asc" ? "desc" : "asc";
    } else {
      state.librarySort = { col, dir: "asc" };
    }
    renderLibrary();
  }

  function renderLibrary() {
    const { col, dir } = state.librarySort;
    const sorted = [...state.savedWorkouts].sort((a, b) => {
      const av = a[col], bv = b[col];
      if (av === bv) return 0;
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return dir === "asc" ? cmp : -cmp;
    });

    $("library-panel").querySelectorAll("th[data-col]").forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      if (th.dataset.col === col) th.classList.add(dir === "asc" ? "sort-asc" : "sort-desc");
    });

    const tbody = $("library-rows");
    tbody.innerHTML = "";
    sorted.forEach((w) => {
      const tr = document.createElement("tr");
      const ifText = w.planned_if !== null && w.planned_if !== undefined ? w.planned_if.toFixed(2) : "--";
      const tssText = w.planned_tss !== null && w.planned_tss !== undefined ? w.planned_tss : "--";
      tr.innerHTML = `
        <td>${w.name}</td>
        <td>${w.goal_label || "--"}</td>
        <td>${w.duration_min} min</td>
        <td>${ifText}</td>
        <td>${tssText}</td>
        <td>
          <button type="button" class="outline load-workout-btn">Load</button>
          <button type="button" class="secondary outline delete-workout-btn">Delete</button>
        </td>
      `;
      tr.querySelector(".load-workout-btn").addEventListener("click", () => renderWorkoutPreview(w));
      tr.querySelector(".delete-workout-btn").addEventListener("click", () => deleteSavedWorkout(w.id));
      tbody.appendChild(tr);
    });

    $("library-empty").hidden = sorted.length > 0;
  }

  async function deleteSavedWorkout(id) {
    if (!confirm("Delete this saved workout?")) return;
    try {
      const resp = await fetch(`/training/saved/${id}/delete`, { method: "POST" });
      const result = await resp.json();
      if (!result.ok) return;
      state.savedWorkouts = state.savedWorkouts.filter((w) => w.id !== id);
      renderLibrary();
    } catch (err) {
      console.error("Delete workout failed", err);
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

  // ---- Workout playback (target power / current step during the ride) ----

  function computePlayback(elapsed) {
    if (!state.workout) return null;
    let t = 0;
    for (const step of state.workout.steps) {
      const stepEnd = t + step.duration_s;
      if (elapsed < stepEnd) {
        return { step, remainingS: Math.ceil(stepEnd - elapsed) };
      }
      t = stepEnd;
    }
    return null; // workout complete
  }

  function updatePlaybackChips() {
    if (state.workout && !state.playback) {
      $("chip-target").textContent = "--";
      $("chip-step").textContent = "Workout complete";
      return;
    }
    if (!state.playback) {
      $("chip-target").textContent = "--";
      $("chip-step").textContent = "--";
      return;
    }
    $("chip-target").textContent = state.playback.step.watts;
    $("chip-step").textContent = `${state.playback.step.label} (${state.playback.remainingS}s left)`;
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

    state.playback = computePlayback(elapsed);
    updatePlaybackChips();
    const targetWatts = state.playback ? state.playback.step.watts : null;
    const currentStepIndex = state.playback ? state.workout.steps.indexOf(state.playback.step) : null;
    if (currentStepIndex !== null && currentStepIndex !== state.lastStepIndex) {
      state.lastStepIndex = currentStepIndex;
      setErgTarget(targetWatts);
    }

    const chart = state.chart;
    const label = elapsed.toFixed(0) + "s";
    chart.data.labels.push(label);
    chart.data.datasets[0].data.push(state.latest.power);
    chart.data.datasets[1].data.push(state.latest.hr);
    chart.data.datasets[2].data.push(state.latest.cadence);
    chart.data.datasets[3].data.push(state.latest.speed);
    chart.data.datasets[4].data.push(targetWatts);

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
          { label: "Target (W)", data: [], borderColor: "#888", borderDash: [4, 4], backgroundColor: "transparent", yAxisID: "y", tension: 0, pointRadius: 0 },
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

  async function startSession() {
    $("pairing-view").hidden = true;
    $("live-view").hidden = false;
    state.startTime = Date.now();
    state.samples = [];
    state.playback = null;
    state.lastStepIndex = null;
    initChart();

    const wantsErg = $("erg-toggle").checked && !$("erg-toggle").disabled;
    if (wantsErg && state.workout && state.trainer.controlChar) {
      await enableErgMode();
    }

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

  async function stopSession() {
    clearInterval(state.updateTimer);
    state.updateTimer = null;
    state.playback = null;
    state.lastStepIndex = null;
    await releaseErgControl(); // must happen before disconnecting the GATT server
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
    $("save-workout-btn").addEventListener("click", saveWorkout);

    $("workout-tabs").querySelectorAll("a[data-mode]").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); switchMode(a.dataset.mode); });
    });
    $("add-step-btn").addEventListener("click", () => addCustomStepRow());
    $("preview-custom-btn").addEventListener("click", previewCustomWorkout);
    $("library-panel").querySelectorAll("th[data-col]").forEach((th) => {
      th.addEventListener("click", () => sortLibrary(th.dataset.col));
    });

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
