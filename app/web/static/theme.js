// The saved light / dark theme, applied before the page is first drawn: this file is
// loaded without defer in <head> (an inline script would need the Content-Security-
// Policy to allow it). The toggle itself is in app.js.
try {
  if (localStorage.getItem("ser2net-theme") === "light")
    document.documentElement.setAttribute("data-theme", "light");
} catch (e) {}
