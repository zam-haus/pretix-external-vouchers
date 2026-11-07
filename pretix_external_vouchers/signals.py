import json
from django.dispatch import receiver
from django.templatetags.static import static
from django.utils.html import format_html
from pretix.base.signals import order_placed, register_payment_providers
from pretix.presale.signals import (
    html_head,
    order_meta_from_request,
    process_response,
)

from .payment import (
    IDENTIFIER,
    META_KEY,
    SESSION_KEY,
    ExternalVoucherPayment,
    confirm_unpaid,
)


@receiver(register_payment_providers, dispatch_uid="payment_external_voucher")
def register_payment_provider(sender, **kwargs):
    return ExternalVoucherPayment


@receiver(order_meta_from_request, dispatch_uid="external_voucher_order_meta")
def order_meta(sender, request, **kwargs):
    # During checkout, execute_payment() is called without a request and the cart's
    # payment info_data is not ours to fill, so we carry the answer over via the
    # order's meta_info.
    from pretix.presale.views.cart import cart_session

    payments = cart_session(request).get("payments", [])
    if any(p.get("provider") == IDENTIFIER for p in payments):
        return {META_KEY: request.session.get(SESSION_KEY, "")}
    return {}


@receiver(order_placed, dispatch_uid="external_voucher_order_placed")
def order_placed_receiver(sender, order, **kwargs):
    # Runs before the order confirmation mail is sent, so the mail already reflects
    # the extended payment deadline and the "valid if pending" state. Orders that
    # require approval get no payment yet and are therefore not touched.
    payments = [p for p in order.payments.all() if p.provider == IDENTIFIER]
    if not payments:
        return
    provider = sender.get_payment_providers(cached=True).get(IDENTIFIER)
    answer = (order.meta_info_data or {}).get(META_KEY, "")
    for p in payments:
        info = p.info_data
        info.setdefault("answer", answer)
        if provider:
            info.setdefault("question", provider.question)
        p.info_data = info
        p.save(update_fields=["info"])
    confirm_unpaid(order)


@receiver(html_head, dispatch_uid="external_voucher_html_head")
def hide_voucher_redemption(sender, request=None, **kwargs):
    # Only hides the "Redeem a voucher" box and cart toggle. Voucher links
    # (/redeem?voucher=...) keep working. The script is a fallback for browsers
    # without CSS :has() support.
    return format_html(
        '<link rel="stylesheet" type="text/css" href="{}">'
        '<script type="text/javascript" src="{}" defer></script>',
        static("pretix_external_vouchers/hide_vouchers.css"),
        static("pretix_external_vouchers/hide_vouchers.js"),
    )


@receiver(process_response, dispatch_uid="external_voucher_widget")
def hide_widget_voucher_field(sender, request, response, **kwargs):
    # The embeddable widget only shows its voucher input if the product list says
    # that vouchers exist. Vouchers passed via the widget's data-voucher attribute
    # do not depend on this flag and keep working.
    match = getattr(request, "resolver_match", None)
    if (
        not match
        or match.url_name != "event.widget.productlist"
        or response.status_code != 200
        or not response.get("Content-Type", "").startswith("application/json")
    ):
        return response
    try:
        data = json.loads(response.content)
    except ValueError:
        return response
    if isinstance(data, dict) and data.get("vouchers_exist"):
        data["vouchers_exist"] = False
        response.content = json.dumps(data)
    return response
