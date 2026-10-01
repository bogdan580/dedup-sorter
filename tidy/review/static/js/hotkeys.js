let lastClickedCardId = null;

function installHotkeys() {
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const lb = document.getElementById("lightbox");
      if (lb && lb.style.display !== "none") {
        lb.style.display = "none";
        document.getElementById("lightbox-img").src = "";
        return;
      }
    }
    
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      const applyBtn = document.getElementById("apply-page-btn");
      if (applyBtn && applyBtn.offsetParent !== null && !applyBtn.disabled) {
        applyBtn.click();
        e.preventDefault();
      }
      return;
    }
    
    const lb = document.getElementById("lightbox");
    if (lb && lb.style.display !== "none") {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        const currentSrc = document.getElementById("lightbox-img").src;
        const cards = Array.from(document.querySelectorAll(".card"));
        let currentIndex = -1;
        for (let i = 0; i < cards.length; i++) {
          const img = cards[i].querySelector("img.thumb");
          if (img && currentSrc.includes(img.src.replace("/thumb/", "/file/"))) {
            currentIndex = i;
            break;
          }
        }
        
        if (currentIndex !== -1) {
          let nextIndex = e.key === "ArrowRight" ? currentIndex + 1 : currentIndex - 1;
          if (nextIndex >= 0 && nextIndex < cards.length) {
            const evt = new MouseEvent('dblclick', { bubbles: true, cancelable: true });
            cards[nextIndex].dispatchEvent(evt);
            cards[nextIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }
        }
      }
    }
  });
  
  // Shift-click support for checkboxes
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('exclude-checkbox')) {
      const card = e.target.closest('.card');
      if (!card) return;
      
      const currentId = card.dataset.id;
      
      if (e.shiftKey && lastClickedCardId) {
        const allCards = Array.from(document.querySelectorAll('.card'));
        const startIndex = allCards.findIndex(c => c.dataset.id === lastClickedCardId);
        const endIndex = allCards.findIndex(c => c.dataset.id === currentId);
        
        if (startIndex !== -1 && endIndex !== -1) {
          const min = Math.min(startIndex, endIndex);
          const max = Math.max(startIndex, endIndex);
          const targetChecked = e.target.checked;
          
          for (let i = min; i <= max; i++) {
            const cb = allCards[i].querySelector('.exclude-checkbox');
            if (cb && cb !== e.target && cb.checked !== targetChecked) {
              cb.checked = targetChecked;
              // Trigger change event to update state
              cb.dispatchEvent(new Event('change', { bubbles: true }));
            }
          }
        }
      }
      lastClickedCardId = currentId;
    }
  }, true);
}

document.addEventListener("DOMContentLoaded", installHotkeys);
