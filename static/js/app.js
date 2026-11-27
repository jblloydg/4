document.addEventListener("DOMContentLoaded", function () {
  var modal = document.getElementById("login-modal");
  var nextField = document.getElementById("login-next");
  var firstInput = modal ? modal.querySelector("input[name='username']") : null;

  function openLogin(nextUrl) {
    if (!modal) return;
    if (nextField) nextField.value = nextUrl || window.location.pathname + window.location.search;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    if (firstInput) firstInput.focus();
  }

  function closeLogin() {
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
  }

  document.querySelectorAll("[data-login-next]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      openLogin(button.getAttribute("data-login-next"));
    });
  });
  document.querySelectorAll("[data-close-login]").forEach(function (button) {
    button.addEventListener("click", closeLogin);
  });
  if (modal) {
    modal.addEventListener("click", function (event) {
      if (event.target === modal) closeLogin();
    });
  }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeLogin();
  });

  if (document.body.getAttribute("data-user-authenticated") === "true") {
    var noticeSocket = new WebSocket(
      (window.location.protocol === "https:" ? "wss:" : "ws:") + "//" + window.location.host + "/ws/notifications/"
    );
    noticeSocket.addEventListener("message", function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (error) { return; }
      if (data.type !== "notification") return;
      var badge = document.querySelector(".notice-count");
      var nav = document.querySelector(".nav-notice");
      if (!badge && nav) {
        badge = document.createElement("span");
        badge.className = "notice-count";
        nav.appendChild(badge);
      }
      if (badge) badge.textContent = String((parseInt(badge.textContent || "0", 10) || 0) + 1);
      var stack = document.querySelector(".flash-stack");
      if (!stack) {
        stack = document.createElement("div");
        stack.className = "flash-stack";
        stack.setAttribute("role", "status");
        stack.setAttribute("aria-live", "polite");
        document.body.appendChild(stack);
      }
      var flash = document.createElement("div");
      flash.className = "flash flash-info";
      flash.textContent = data.text;
      stack.appendChild(flash);
      window.setTimeout(function () { flash.remove(); }, 6500);
    });
  }

  var typeSelect = document.querySelector("[data-post-type]");
  var categorySelect = document.querySelector("[data-category]");
  var questionBlock = document.getElementById("verification-questions");
  var questionList = document.getElementById("question-list");
  var questionSearch = document.getElementById("question-search");
  var pickedLabel = document.getElementById("question-picked");
  var suggestNote = document.getElementById("question-suggest-note");
  var MAX_QUESTIONS = 5;

  function updateQuestions() {
    if (!typeSelect || !questionBlock) return;
    questionBlock.hidden = typeSelect.value !== "FOUND";
  }
  function questionOptions() {
    return questionList ? Array.prototype.slice.call(questionList.querySelectorAll(".question-option")) : [];
  }
  function updatePicked() {
    var options = questionOptions();
    var checked = options.filter(function (o) { return o.querySelector("input").checked; });
    options.forEach(function (o) {
      var box = o.querySelector("input");
      o.classList.toggle("is-checked", box.checked);
      box.disabled = !box.checked && checked.length >= MAX_QUESTIONS;
    });
    if (pickedLabel) {
      var n = checked.length;
      pickedLabel.textContent = n + " of " + MAX_QUESTIONS + " picked" + (n < 3 ? " · " + (3 - n) + " default" + (3 - n === 1 ? "" : "s") + " will be added" : "");
    }
  }
  function sortByCategory() {
    if (!questionList) return;
    var categoryId = categorySelect ? categorySelect.value : "";
    var options = questionOptions();
    var anySuggested = false;
    options.forEach(function (o) {
      var ids = (o.getAttribute("data-categories") || "").split(" ");
      var suggested = !!categoryId && ids.indexOf(categoryId) !== -1;
      o.classList.toggle("is-suggested", suggested);
      if (suggested) anySuggested = true;
    });
    options.sort(function (a, b) {
      var rank = function (o) { return o.classList.contains("is-suggested") ? 0 : (o.getAttribute("data-general") === "1" ? 1 : 2); };
      return rank(a) - rank(b);
    }).forEach(function (o) { questionList.insertBefore(o, document.getElementById("question-empty")); });
    if (suggestNote) suggestNote.hidden = !anySuggested;
  }
  function filterQuestions() {
    var term = questionSearch ? questionSearch.value.trim().toLowerCase() : "";
    var shown = 0;
    questionOptions().forEach(function (o) {
      var match = !term || o.textContent.toLowerCase().indexOf(term) !== -1;
      o.hidden = !match;
      if (match) shown += 1;
    });
    var empty = document.getElementById("question-empty");
    if (empty) empty.hidden = shown !== 0;
  }
  if (typeSelect) {
    typeSelect.addEventListener("change", updateQuestions);
    updateQuestions();
  }
  if (questionList) {
    questionList.addEventListener("change", updatePicked);
    if (questionSearch) {
      questionSearch.addEventListener("input", filterQuestions);
      questionSearch.addEventListener("keydown", function (event) { if (event.key === "Enter") event.preventDefault(); });
    }
    if (categorySelect) categorySelect.addEventListener("change", sortByCategory);
    sortByCategory();
    updatePicked();
  }

  var photoInput = document.querySelector("input[type='file'][name='photos']");
  var humanCheck = document.getElementById("human-check");
  var photoCount = document.getElementById("photo-count");
  function updatePhotoState() {
    if (!photoInput) return;
    var existing = humanCheck ? parseInt(humanCheck.getAttribute("data-existing") || "0", 10) : 0;
    var removing = document.querySelectorAll("input[name='remove_photos']:checked").length;
    var adding = photoInput.files ? photoInput.files.length : 0;
    var total = existing - removing + adding;
    if (photoCount) photoCount.textContent = total ? total + " of 20 photos" + (total > 20 ? " — remove some to continue" : "") : "";
    if (humanCheck) {
      var threshold = parseInt(humanCheck.getAttribute("data-threshold") || "10", 10);
      var needed = adding > 0 && total > threshold;
      humanCheck.hidden = !needed && !humanCheck.querySelector(".field-error");
      var answer = humanCheck.querySelector("input");
      if (answer) answer.required = needed;
    }
  }
  if (photoInput) {
    photoInput.addEventListener("change", updatePhotoState);
    document.querySelectorAll("input[name='remove_photos']").forEach(function (box) { box.addEventListener("change", updatePhotoState); });
    updatePhotoState();
  }

  var params = new URLSearchParams(window.location.search);
  if (params.get("resume") === "claim") {
    var claim = document.getElementById("claim-form");
    if (claim) {
      claim.scrollIntoView({ behavior: "smooth", block: "center" });
      var firstAnswer = claim.querySelector("select, textarea");
      if (firstAnswer) firstAnswer.focus({ preventScroll: true });
    }
  }
  if (params.get("resume") === "comment") {
    var comment = document.querySelector(".comment-composer textarea");
    if (comment) {
      comment.focus({ preventScroll: true });
      comment.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  var dismissible = document.querySelectorAll(".flash");
  dismissible.forEach(function (flash) {
    window.setTimeout(function () {
      flash.style.opacity = "0";
      flash.style.transition = "opacity .35s ease";
      window.setTimeout(function () { flash.remove(); }, 400);
    }, 5200);
  });
});
