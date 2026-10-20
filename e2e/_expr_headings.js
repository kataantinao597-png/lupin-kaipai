(() => {
  const hs = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")];
  const levels = hs.map(h => +h.tagName[1]);
  let skips = [];
  for (let i = 1; i < levels.length; i++) {
    if (levels[i] - levels[i - 1] > 1) skips.push(hs[i - 1].tagName + "→" + hs[i].tagName + " (" + hs[i].textContent.trim().slice(0, 12) + ")");
  }
  const og = [...document.querySelectorAll('meta[property^="og:"], meta[name="twitter:card"]')]
    .map(m => m.getAttribute("property") || m.getAttribute("name"));
  const iconEl = document.querySelector("link[rel=icon]");
  const canEl = document.querySelector("link[rel=canonical]");
  return JSON.stringify({
    count: hs.length,
    h1: hs.filter(h => h.tagName === "H1").length,
    levels: levels.join(","),
    skips,
    og,
    canonical: canEl ? canEl.href : "",
    icon: iconEl ? iconEl.getAttribute("href") : "",
    title: document.title
  });
})()
