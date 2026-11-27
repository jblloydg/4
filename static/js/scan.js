/* Guard-house QR scanning with an offline queue.
 *
 * Online: a scanned code opens the staff "Confirm turned in" screen.
 * Offline: the code is saved in this browser's storage and listed under
 * "Queued scans". When the connection returns, staff open each queued code
 * and confirm it by hand. Nothing is ever confirmed automatically, and the
 * server checks every code again when it is opened.
 */
document.addEventListener("DOMContentLoaded", function () {
  var QUEUE_KEY = "findit-offline-turn-ins";
  var video = document.getElementById("scan-video");
  var canvas = document.getElementById("scan-canvas");
  var placeholder = document.getElementById("scan-placeholder");
  var startBtn = document.getElementById("scan-start");
  var stopBtn = document.getElementById("scan-stop");
  var statusEl = document.getElementById("scan-status");
  var manualForm = document.getElementById("manual-code-form");
  var manualInput = document.getElementById("manual-code");
  var queueBox = document.getElementById("offline-queue");
  var queueList = document.getElementById("queue-list");
  var queueClear = document.getElementById("queue-clear");
  var stream = null;
  var detector = null;
  var scanning = false;
  var lastCode = "";

  function say(text) { if (statusEl) statusEl.textContent = text; }

  function readQueue() {
    try { return JSON.parse(window.localStorage.getItem(QUEUE_KEY) || "[]"); } catch (e) { return []; }
  }
  function writeQueue(items) {
    try { window.localStorage.setItem(QUEUE_KEY, JSON.stringify(items)); } catch (e) {}
  }
  function turnInUrl(code) {
    return window.FINDIT_TURN_IN_URL.replace("CODE", encodeURIComponent(code)) + "?via=qr";
  }
  function renderQueue() {
    var items = readQueue();
    if (!queueBox) return;
    queueBox.hidden = items.length === 0;
    queueList.innerHTML = "";
    items.forEach(function (item, index) {
      var li = document.createElement("li");
      var label = document.createElement("span");
      label.textContent = item.code + " · scanned " + new Date(item.at).toLocaleString();
      var open = document.createElement("a");
      open.className = "button button-small " + (navigator.onLine ? "button-primary" : "button-outline");
      open.textContent = navigator.onLine ? "Open & confirm" : "Waiting for internet";
      open.href = turnInUrl(item.code);
      open.addEventListener("click", function (event) {
        if (!navigator.onLine) { event.preventDefault(); return; }
        var rest = readQueue();
        rest.splice(index, 1);
        writeQueue(rest);
      });
      li.append(label, open);
      queueList.appendChild(li);
    });
  }

  // Pull the turn-in code out of a scanned URL (…/staff/turn-in/CODE/) or a bare code.
  function extractCode(text) {
    var match = /\/staff\/turn-in\/([A-Za-z0-9]{4,12})\/?/.exec(text || "");
    if (match) return match[1].toUpperCase();
    var bare = (text || "").trim().toUpperCase();
    return /^[A-Z0-9]{4,12}$/.test(bare) ? bare : "";
  }

  function handleCode(code, source) {
    if (!code || code === lastCode) return;
    lastCode = code;
    window.setTimeout(function () { lastCode = ""; }, 4000);
    if (!navigator.onLine) {
      var items = readQueue();
      if (!items.some(function (i) { return i.code === code; })) {
        items.push({ code: code, at: new Date().toISOString(), source: source });
        writeQueue(items);
      }
      say("No internet. Saved " + code + " to the queue; confirm it when you're back online.");
      renderQueue();
      return;
    }
    stop();
    say("Found " + code + ". Opening the confirm screen…");
    window.location.href = turnInUrl(code);
  }

  function tick() {
    if (!scanning) return;
    if (video.readyState === video.HAVE_ENOUGH_DATA) {
      if (detector) {
        detector.detect(video).then(function (codes) {
          if (codes && codes.length) handleCode(extractCode(codes[0].rawValue), "camera");
        }).catch(function () {});
      } else if (window.jsQR) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        var ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        var frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
        var result = window.jsQR(frame.data, frame.width, frame.height, { inversionAttempts: "dontInvert" });
        if (result) handleCode(extractCode(result.data), "camera");
      }
    }
    window.setTimeout(function () { window.requestAnimationFrame(tick); }, 180);
  }

  function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      say("This browser can't open the camera here. Use the code box below. (Camera needs localhost or HTTPS.)");
      return;
    }
    if ("BarcodeDetector" in window) {
      try { detector = new window.BarcodeDetector({ formats: ["qr_code"] }); } catch (e) { detector = null; }
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (s) {
      stream = s;
      video.srcObject = s;
      video.hidden = false;
      placeholder.hidden = true;
      startBtn.hidden = true;
      stopBtn.hidden = false;
      return video.play();
    }).then(function () {
      scanning = true;
      say(navigator.onLine ? "Scanning… hold the QR code steady." : "Scanning offline: codes will be queued.");
      tick();
    }).catch(function () {
      say("Camera permission was blocked or no camera was found. Use the code box below.");
    });
  }

  function stop() {
    scanning = false;
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null;
    if (video) video.hidden = true;
    if (placeholder) placeholder.hidden = false;
    if (startBtn) startBtn.hidden = false;
    if (stopBtn) stopBtn.hidden = true;
  }

  if (startBtn) startBtn.addEventListener("click", start);
  if (stopBtn) stopBtn.addEventListener("click", function () { stop(); say("Camera stopped."); });
  if (manualForm) manualForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var code = extractCode(manualInput.value);
    if (!code) { say("That doesn't look like a turn-in code."); return; }
    lastCode = "";
    handleCode(code, "typed");
  });
  if (queueClear) queueClear.addEventListener("click", function () { writeQueue([]); renderQueue(); });
  window.addEventListener("online", function () {
    renderQueue();
    if (readQueue().length) say("Back online. Open each queued scan to confirm it.");
  });
  window.addEventListener("offline", function () { say("You're offline. Scans will be queued on this device."); renderQueue(); });
  renderQueue();
});
