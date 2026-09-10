function showBookmarkInstructions() {
  var el = document.getElementById("bookmark-instructions");
  var ua = navigator.userAgent || "";
  var isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
  var isAndroid = /Android/.test(ua);
  var isMac = /Macintosh/.test(ua) && !isIOS;

  var text;
  if (isIOS) {
    text = "Tap the Share icon at the bottom of Safari (the square with an arrow), then choose \"Add to Home Screen.\"";
  } else if (isAndroid) {
    text = "Tap the ⋮ menu in the top right of Chrome, then choose \"Add to Home screen\" (or \"Install app\" if that's shown instead).";
  } else if (isMac) {
    text = "Press Cmd+D to bookmark this page in your browser.";
  } else {
    text = "Press Ctrl+D to bookmark this page in your browser.";
  }
  el.textContent = text;
  el.hidden = false;
}
