(async () => {
  const nav = document.querySelector('.nav-item[data-v="v9"]');
  if (nav) nav.click();
  await new Promise(r => setTimeout(r, 1800));
  const v = document.getElementById("v9");
  return JSON.stringify({
    h: v ? Math.round(v.getBoundingClientRect().height) : -1,
    text: v ? v.innerText.replace(/\n+/g, " / ").slice(0, 300) : "",
    cards: v ? v.querySelectorAll(".card").length : -1,
    empty: v ? !!v.querySelector(".empty, [class*=empty]") : false
  });
})()
