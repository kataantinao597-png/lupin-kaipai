(function(){
  "use strict";
  document.documentElement.classList.add("js");

  /* 图片加载失败降级（CSP 安全：不用内联 onerror） */
  document.addEventListener("error", function(e){
    var t = e.target;
    if(!t || (t.tagName !== "IMG" && t.tagName !== "SOURCE")) return;
    var img = t.tagName === "SOURCE" ? t.parentElement.querySelector("img") : t;
    if(!img) return;
    /* 等本轮资源结算后再判定，避免首个 source 失败时误判 */
    setTimeout(function(){
      if(img.complete && img.naturalWidth === 0){
        if(img.getAttribute("data-hero")){
          var hero = img.closest(".hero");
          if(hero) hero.classList.add("noimg");
        } else {
          img.style.display = "none";
        }
      }
    }, 0);
  }, true);
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* 导航滚动增强 */
  var nav = document.getElementById("nav");
  function onScrollNav(){ nav.classList.toggle("scrolled", window.scrollY > 24); }
  window.addEventListener("scroll", onScrollNav, {passive:true});
  onScrollNav();

  /* 移动端菜单 */
  var burger = document.getElementById("burger");
  var links = document.getElementById("navLinks");
  burger.addEventListener("click", function(){
    var open = links.classList.toggle("open");
    burger.setAttribute("aria-expanded", open ? "true" : "false");
  });
  links.addEventListener("click", function(e){
    if(e.target.tagName === "A"){ links.classList.remove("open"); burger.setAttribute("aria-expanded", "false"); }
  });

  /* Hero 轻微视差（仅桌面端） */
  var heroMedia = document.getElementById("heroMedia");
  var fine = window.matchMedia("(min-width: 961px)").matches;
  if(heroMedia && fine && !reduced){
    var ticking = false;
    window.addEventListener("scroll", function(){
      if(ticking) return; ticking = true;
      requestAnimationFrame(function(){
        var y = window.scrollY;
        if(y < window.innerHeight) heroMedia.style.transform = "translateY(" + (y * 0.22) + "px)";
        ticking = false;
      });
    }, {passive:true});
  }

  /* Hero 视频背景：自动播放被拒/加载失败/减少动效时回退静态封面，绝不黑屏 */
  var heroVideo = document.getElementById("heroVideo");
  if(heroVideo){
    /* 移动端减负（桌面端保持原样）：窄屏或触屏设备不自动播放、只加载元数据，优先显示静态封面 */
    var mobileVideo = window.matchMedia("(max-width: 768px)").matches ||
                      window.matchMedia("(pointer: coarse)").matches;
    if(reduced){
      /* 减少动效：直接移除视频，只留封面图 */
      heroVideo.remove();
    } else if(mobileVideo){
      heroVideo.removeAttribute("autoplay");
      heroVideo.preload = "metadata";
      heroVideo.pause();
      /* 保留 poster 静态封面展示；加载失败时隐藏视频露出底层封面图 */
      heroVideo.addEventListener("error", function(){ heroVideo.style.display = "none"; }, true);
    } else {
      heroVideo.addEventListener("error", function(){ heroVideo.style.display = "none"; }, true);
      var hpr = heroVideo.play();
      if(hpr && hpr.catch){ hpr.catch(function(){ heroVideo.style.display = "none"; }); }
    }
  }

  /* CTA 视频：接近视口时才加载；离开视口暂停，减少首屏流量 */
  var ctaVideo = document.querySelector(".cta-video");
  if(ctaVideo && !reduced){
    var ctaSource = ctaVideo.querySelector("source[data-src]");
    var ctaLoaded = false;
    function loadCtaVideo(){
      if(ctaLoaded || !ctaSource) return;
      ctaSource.setAttribute("src", ctaSource.getAttribute("data-src"));
      ctaSource.removeAttribute("data-src");
      ctaVideo.load();
      ctaLoaded = true;
    }
    if("IntersectionObserver" in window){
      var wasVisible = false;
      var vio = new IntersectionObserver(function(entries){
        entries.forEach(function(en){
          if(en.isIntersecting){
            wasVisible = true;
            loadCtaVideo();
            var pr = ctaVideo.play();
            if(pr && pr.catch) pr.catch(function(){});
          } else if(wasVisible){
            wasVisible = false;
            ctaVideo.pause();
          }
        });
      }, {threshold: 0.05, rootMargin: "180px 0px"});
      vio.observe(ctaVideo);
    } else {
      loadCtaVideo();
    }
  }

  /* 滚动显现 */
  var revealEls = document.querySelectorAll(".reveal");
  if(!("IntersectionObserver" in window) || reduced){
    revealEls.forEach(function(el){ el.classList.add("in"); });
  } else {
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if(en.isIntersecting){ en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, {threshold: 0.12, rootMargin: "0px 0px -40px 0px"});
    revealEls.forEach(function(el){ io.observe(el); });
  }

  /* 流程：滚动高亮 + 连接线填充 */
  var steps = Array.prototype.slice.call(document.querySelectorAll(".fstep"));
  var fill = document.getElementById("flowFill");
  function paintFlow(){
    var lit = steps.filter(function(s){ return s.classList.contains("lit"); }).length;
    var pct = steps.length > 1 ? (lit - 1) / (steps.length - 1) * 100 : 0;
    // 动效标准#7：动画 transform 而非 width（避免逐帧重排）
    if(fill) fill.style.transform = "scaleX(" + (pct / 100) + ")";
  }
  if("IntersectionObserver" in window && !reduced){
    var fio = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if(en.isIntersecting){ en.target.classList.add("lit"); paintFlow(); }
      });
    }, {threshold: 0.6});
    steps.forEach(function(s){ fio.observe(s); });
  } else {
    steps.forEach(function(s){ s.classList.add("lit"); });
    paintFlow();
  }
})();