// Fade-in on scroll
document.addEventListener("DOMContentLoaded", () => {
  const elements = document.querySelectorAll("h2, table, .card, .fixtures");
  function animateOnScroll() {
    elements.forEach(el => {
      const rect = el.getBoundingClientRect();
      if (rect.top <= window.innerHeight - 100) {
        el.classList.add("visible");
      }
    });
  }
  window.addEventListener("scroll", animateOnScroll);
  animateOnScroll();
});

// Parallax logo
window.addEventListener("scroll", () => {
  const logo = document.querySelector("header img");
  if (logo) {
    logo.style.transform = `translateY(${window.scrollY * 0.2}px)`;
  }
});
