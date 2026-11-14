/**
 * RoomLoop Main Interactive Script
 * Lightweight vanilla JS / Alpine.js handlers for drawer, dropdowns, modals, toasts.
 */

document.addEventListener("DOMContentLoaded", function () {
  // 1. Mobile Sidebar Drawer Toggle
  const sidebar = document.getElementById("app-sidebar");
  const sidebarToggle = document.getElementById("mobile-sidebar-toggle");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");

  function openSidebar() {
    if (sidebar) sidebar.classList.add("mobile-open");
    if (sidebarBackdrop) sidebarBackdrop.classList.remove("hidden");
  }

  function closeSidebar() {
    if (sidebar) sidebar.classList.remove("mobile-open");
    if (sidebarBackdrop) sidebarBackdrop.classList.add("hidden");
  }

  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", function (e) {
      e.stopPropagation();
      openSidebar();
    });
  }

  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", closeSidebar);
  }

  // 2. User Profile Dropdown
  const userMenuBtn = document.getElementById("user-menu-button");
  const userMenuDropdown = document.getElementById("user-menu-dropdown");

  if (userMenuBtn && userMenuDropdown) {
    userMenuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      userMenuDropdown.classList.toggle("hidden");
    });

    document.addEventListener("click", function (e) {
      if (!userMenuDropdown.contains(e.target) && !userMenuBtn.contains(e.target)) {
        userMenuDropdown.classList.add("hidden");
      }
    });
  }

  // 3. Password visibility toggle
  const togglePasswordBtns = document.querySelectorAll("[data-toggle-password]");
  togglePasswordBtns.forEach((btn) => {
    btn.addEventListener("click", function () {
      const targetId = this.getAttribute("data-toggle-password");
      const targetInput = document.getElementById(targetId);
      if (targetInput) {
        if (targetInput.type === "password") {
          targetInput.type = "text";
          this.setAttribute("aria-label", "Hide password");
        } else {
          targetInput.type = "password";
          this.setAttribute("aria-label", "Show password");
        }
      }
    });
  });

  // 4. Auto-dismiss Toast Messages
  const toastElements = document.querySelectorAll("[data-toast]");
  toastElements.forEach((toast) => {
    setTimeout(() => {
      toast.style.transition = "opacity 0.3s ease, transform 0.3s ease";
      toast.style.opacity = "0";
      toast.style.transform = "translateY(-10px)";
      setTimeout(() => toast.remove(), 300);
    }, 5000);

    const closeBtn = toast.querySelector("[data-close-toast]");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        toast.remove();
      });
    }
  });

  // 5. Interactive Demo Account Quick-Fill on Login Page
  const quickLoginBtns = document.querySelectorAll("[data-quick-login-email]");
  quickLoginBtns.forEach((btn) => {
    btn.addEventListener("click", function () {
      const email = this.getAttribute("data-quick-login-email");
      const pass = this.getAttribute("data-quick-login-password") || "RoomLoop2026!";
      const emailInput = document.getElementById("id_email");
      const passInput = document.getElementById("id_password");

      if (emailInput && passInput) {
        emailInput.value = email;
        passInput.value = pass;

        // Visual flash feedback
        emailInput.classList.add("ring-2", "ring-teal-500", "bg-teal-50/50");
        passInput.classList.add("ring-2", "ring-teal-500", "bg-teal-50/50");

        setTimeout(() => {
          emailInput.classList.remove("ring-2", "ring-teal-500", "bg-teal-50/50");
          passInput.classList.remove("ring-2", "ring-teal-500", "bg-teal-50/50");
        }, 600);
      }
    });
  });

  // 6. Real-time Client Table Search Filter
  const tableSearchInputs = document.querySelectorAll("[data-table-filter]");
  tableSearchInputs.forEach((input) => {
    input.addEventListener("input", function () {
      const targetTableId = this.getAttribute("data-table-filter");
      const query = this.value.toLowerCase().trim();
      const table = document.getElementById(targetTableId);
      if (!table) return;

      const rows = table.querySelectorAll("tbody tr");
      rows.forEach((row) => {
        const text = row.textContent.toLowerCase();
        if (text.includes(query)) {
          row.style.display = "";
        } else {
          row.style.display = "none";
        }
      });
    });
  });

  // 7. Interactive Category Filter for Cards Grid
  const cardFilterBtns = document.querySelectorAll("[data-card-filter]");
  cardFilterBtns.forEach((btn) => {
    btn.addEventListener("click", function () {
      const targetGridId = this.getAttribute("data-target-grid");
      const category = this.getAttribute("data-card-filter");
      const grid = document.getElementById(targetGridId);
      if (!grid) return;

      // Update button active state
      const siblingBtns = this.parentElement.querySelectorAll("[data-card-filter]");
      siblingBtns.forEach((b) => {
        b.classList.remove("bg-teal-700", "text-white", "shadow-sm");
        b.classList.add("bg-white", "text-slate-600", "hover:bg-slate-50");
      });
      this.classList.remove("bg-white", "text-slate-600", "hover:bg-slate-50");
      this.classList.add("bg-teal-700", "text-white", "shadow-sm");

      // Filter cards
      const cards = grid.querySelectorAll("[data-category]");
      cards.forEach((card) => {
        const cardCat = card.getAttribute("data-category");
        if (category === "all" || cardCat === category) {
          card.style.display = "";
        } else {
          card.style.display = "none";
        }
      });
    });
  });
});
