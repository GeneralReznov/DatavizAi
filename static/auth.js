/**
 * auth.js — Login, signup, and OTP verification flow for DataVizAI.
 *
 * Works with the tab layout in login.html and the Flask auth blueprint at /auth/*.
 */

document.addEventListener("DOMContentLoaded", () => {
  // ── Auto-switch to signup tab if ?tab=signup in URL ───────────────────────
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get("tab") === "signup") {
    const tabSignupBtn = document.getElementById("tab-signup");
    if (tabSignupBtn) new bootstrap.Tab(tabSignupBtn).show();
  }

  // ── Element refs ──────────────────────────────────────────────────────────
  const formLogin  = document.getElementById("form-login");
  const formSignup = document.getElementById("form-signup");
  const formOtp    = document.getElementById("form-otp");

  const errLogin  = document.getElementById("login-error");
  const errSignup = document.getElementById("signup-error");
  const errOtp    = document.getElementById("otp-error");

  const tabLogin  = document.getElementById("tab-login");
  const tabSignup = document.getElementById("tab-signup");
  const tabOtp    = document.getElementById("tab-otp");
  const liOtp     = document.getElementById("li-otp");

  const otpUserIdInput = document.getElementById("otp-user-id");
  const otpTimerEl     = document.getElementById("otp-timer");
  const btnResend      = document.getElementById("btn-resend");

  let timerInterval = null;

  // ── Helpers ───────────────────────────────────────────────────────────────

  function showError(el, msg) {
    el.textContent = msg;
    el.classList.remove("d-none");
  }

  function hideError(el) {
    el.classList.add("d-none");
    el.textContent = "";
  }

  function setLoading(btn, loading) {
    btn.disabled = loading;
    btn.innerHTML = loading
      ? '<span class="spinner-border spinner-border-sm me-1"></span> Please wait…'
      : btn.dataset.label || btn.innerHTML;
  }

  // Save original button labels
  [formLogin, formSignup, formOtp].forEach(f => {
    const btn = f ? f.querySelector("button[type=submit]") : null;
    if (btn) btn.dataset.label = btn.innerHTML;
  });

  // ── OTP countdown timer ───────────────────────────────────────────────────

  function startTimer(seconds = 600) {
    clearInterval(timerInterval);
    btnResend.disabled = true;
    let remaining = seconds;

    function tick() {
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      otpTimerEl.textContent = `${m}:${s < 10 ? "0" : ""}${s}`;
      remaining--;
      if (remaining < 0) {
        clearInterval(timerInterval);
        otpTimerEl.textContent = "00:00";
        btnResend.disabled = false;
      }
    }
    tick();
    timerInterval = setInterval(tick, 1000);
  }

  function switchToOtpTab(userId) {
    otpUserIdInput.value = userId;
    // Hide login/signup tabs, show OTP tab
    tabLogin.parentElement.classList.add("d-none");
    tabSignup.parentElement.classList.add("d-none");
    liOtp.classList.remove("d-none");
    const otpTabInstance = new bootstrap.Tab(tabOtp);
    otpTabInstance.show();
    startTimer(600);
  }

  // ── Login form ────────────────────────────────────────────────────────────

  formLogin.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError(errLogin);
    const submitBtn = formLogin.querySelector("button[type=submit]");
    setLoading(submitBtn, true);

    const email    = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;

    try {
      const res  = await fetch("/auth/login", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ email, password }),
      });
      const data = await res.json();

      if (res.ok) {
        // Redirect based on role
        window.location.href = data.role === "admin" ? "/admin/dashboard" : "/";
      } else if (data.unverified && data.userId) {
        // Account not yet OTP-verified — switch to OTP tab
        switchToOtpTab(data.userId);
        showError(errLogin, data.error);
      } else {
        showError(errLogin, data.error || "Login failed. Please try again.");
      }
    } catch (err) {
      showError(errLogin, "Network error. Please try again.");
    } finally {
      setLoading(submitBtn, false);
    }
  });

  // ── Signup form ───────────────────────────────────────────────────────────

  formSignup.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError(errSignup);

    const name     = document.getElementById("signup-name").value.trim();
    const email    = document.getElementById("signup-email").value.trim();
    const phone    = document.getElementById("signup-phone").value.trim();
    const password = document.getElementById("signup-password").value;
    const confirm  = document.getElementById("signup-confirm").value;

    if (password !== confirm) {
      showError(errSignup, "Passwords do not match.");
      return;
    }
    if (password.length < 6) {
      showError(errSignup, "Password must be at least 6 characters.");
      return;
    }

    const submitBtn = formSignup.querySelector("button[type=submit]");
    setLoading(submitBtn, true);

    try {
      const res  = await fetch("/auth/signup", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ name, email, phone, password }),
      });
      const data = await res.json();

      if (res.ok) {
        switchToOtpTab(data.userId);
      } else {
        showError(errSignup, data.error || "Registration failed. Please try again.");
      }
    } catch (err) {
      showError(errSignup, "Network error. Please try again.");
    } finally {
      setLoading(submitBtn, false);
    }
  });

  // ── OTP form ──────────────────────────────────────────────────────────────

  formOtp.addEventListener("submit", async (e) => {
    e.preventDefault();
    hideError(errOtp);

    const userId = otpUserIdInput.value;
    const otp    = document.getElementById("otp-code").value.trim();

    if (otp.length !== 6 || !/^\d+$/.test(otp)) {
      showError(errOtp, "Please enter the 6-digit numeric OTP.");
      return;
    }

    const submitBtn = formOtp.querySelector("button[type=submit]");
    setLoading(submitBtn, true);

    try {
      const res  = await fetch("/auth/verify-otp", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ userId, otp }),
      });
      const data = await res.json();

      if (res.ok) {
        clearInterval(timerInterval);
        window.location.href = data.role === "admin" ? "/admin/dashboard" : "/";
      } else {
        showError(errOtp, data.error || "OTP verification failed.");
      }
    } catch (err) {
      showError(errOtp, "Network error. Please try again.");
    } finally {
      setLoading(submitBtn, false);
    }
  });

  // ── Resend OTP ────────────────────────────────────────────────────────────

  btnResend.addEventListener("click", async () => {
    hideError(errOtp);
    const userId = otpUserIdInput.value;
    btnResend.disabled = true;

    try {
      const res  = await fetch("/auth/resend-otp", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ userId }),
      });
      const data = await res.json();

      if (res.ok) {
        document.getElementById("otp-code").value = "";
        startTimer(600);
      } else {
        showError(errOtp, data.error || "Could not resend OTP.");
        btnResend.disabled = false;
      }
    } catch (err) {
      showError(errOtp, "Network error. Please try again.");
      btnResend.disabled = false;
    }
  });
});
