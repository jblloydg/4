document.addEventListener("DOMContentLoaded", function () {
  var panel = document.getElementById("chat-messages");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  if (!panel || !form || !input) return;

  var conversationId = panel.getAttribute("data-conversation");
  var canSend = panel.getAttribute("data-can-send") === "true";
  var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  var socket = new WebSocket(protocol + "//" + window.location.host + "/ws/conversations/" + conversationId + "/");
  var connectionText = null;

  function formatTime(value) {
    var date = new Date(value);
    return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function addMessage(message) {
    if (panel.querySelector('[data-message-id="' + message.id + '"]')) return;
    var empty = panel.querySelector(".chat-empty");
    if (empty) empty.remove();
    var article = document.createElement("article");
    article.className = "chat-message" + (String(message.sender_id) === document.body.getAttribute("data-user-id") ? " mine" : "");
    article.setAttribute("data-message-id", message.id);
    var bubble = document.createElement("div");
    bubble.className = "message-bubble";
    var meta = document.createElement("div");
    meta.className = "message-meta";
    var name = document.createElement("strong");
    name.textContent = message.sender;
    var time = document.createElement("time");
    time.textContent = formatTime(message.sent_at);
    var text = document.createElement("p");
    text.textContent = message.text;
    meta.append(name, time);
    bubble.append(meta, text);
    article.appendChild(bubble);
    panel.appendChild(article);
    panel.scrollTop = panel.scrollHeight;
  }

  socket.addEventListener("open", function () {
    if (connectionText) connectionText.textContent = "Live chat connected.";
  });
  socket.addEventListener("message", function (event) {
    var data;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    if (data.type === "message") addMessage(data);
    if (data.type === "error") {
      if (connectionText) connectionText.textContent = data.message;
    }
  });
  socket.addEventListener("close", function () {
    if (connectionText) connectionText.textContent = "Live connection ended. Reload the page to reconnect.";
  });
  socket.addEventListener("error", function () {
    if (connectionText) connectionText.textContent = "Live chat could not connect. Check that the app server is running.";
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text || !canSend || socket.readyState !== WebSocket.OPEN) {
      if (connectionText && socket.readyState !== WebSocket.OPEN) connectionText.textContent = "Waiting for the live connection…";
      return;
    }
    socket.send(JSON.stringify({ message: text }));
    input.value = "";
    input.focus();
  });
});
