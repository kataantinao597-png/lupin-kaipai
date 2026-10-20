(async () => {
  const out = {};
  const nav = document.querySelector('.nav-item[data-v="v5"]');
  if (nav) nav.click();
  await new Promise(r => setTimeout(r, 1400));

  out.extraInputs = ["live_platform", "live_goal", "live_style", "live_promo"]
    .map(id => id + (document.getElementById(id) ? ":ok" : ":MISSING"));
  out.status = (document.getElementById("live_status") || {}).textContent || "";

  const segs = [...document.querySelectorAll("#live_result .live-seg")];
  out.segCount = segs.length;
  out.segPreview = segs.slice(0, 3).map(e => e.innerText.replace(/\n+/g, " / ").slice(0, 110));
  out.hasPending = !!document.querySelector("#live_result .live-pending, #live_result li");
  out.regenBtns = document.querySelectorAll("#live_result [id^='live_regen_']").length;

  const box = document.getElementById("live_result");
  out.resultText = box ? box.innerText.replace(/\n+/g, " / ").slice(0, 260) : "";

  const de = document.documentElement;
  out.hscroll = de.scrollWidth > de.clientWidth + 1;
  return JSON.stringify(out);
})()
