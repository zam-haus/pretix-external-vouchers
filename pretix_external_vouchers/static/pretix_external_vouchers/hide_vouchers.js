/*
 * Fallback for browsers without CSS :has() support: hide the "Redeem a voucher"
 * toggle in the cart box. Voucher links (/redeem?voucher=...) keep working.
 */
(function () {
    "use strict";

    function hide() {
        var forms = document.querySelectorAll('form[action$="/cart/voucher"]');
        for (var i = 0; i < forms.length; i++) {
            var container = forms[i].closest(".toggle-container") || forms[i];
            container.hidden = true;
            container.style.display = "none";
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", hide);
    } else {
        hide();
    }
    if (window.MutationObserver) {
        new MutationObserver(hide).observe(document.documentElement, {
            childList: true,
            subtree: true,
        });
    }
})();
