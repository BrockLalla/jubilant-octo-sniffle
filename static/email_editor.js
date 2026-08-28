document.addEventListener("DOMContentLoaded", function () {
  var editor = document.getElementById("email_body_editor");
  var hidden = document.getElementById("email_body");
  if (!editor || !hidden) return;

  function syncHidden() {
    hidden.value = editor.innerHTML;
  }

  document.querySelectorAll(".rte-btn").forEach(function (btn) {
    // Without this, clicking the button steals focus from the editor first,
    // which collapses whatever text was selected -- so "Bold" would have
    // nothing left to apply to by the time the click handler runs.
    btn.addEventListener("mousedown", function (e) {
      e.preventDefault();
    });
    btn.addEventListener("click", function () {
      document.execCommand(btn.dataset.cmd, false, null);
      syncHidden();
    });
  });

  editor.addEventListener("input", syncHidden);

  var form = editor.closest("form");
  if (form) {
    form.addEventListener("submit", syncHidden);
  }

  syncHidden();
});
