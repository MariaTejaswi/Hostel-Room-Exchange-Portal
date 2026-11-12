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
});
