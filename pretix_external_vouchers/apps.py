from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2025.1 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_external_vouchers"
    verbose_name = "External Vouchers"

    class PretixPluginMeta:
        name = gettext_lazy("External Vouchers")
        author = "Julian Hammer"
        description = gettext_lazy(
            "Payment method for external vouchers: asks a question during checkout, "
            "checks the answer's format, records the answer on the payment, marks the "
            "order as confirmed (valid if pending) and extends the payment deadline "
            "until the end of the event. Also hides the built-in 'Redeem a voucher' box."
        )
        visible = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=2025.1.0"
        settings_links = []
        navigation_links = []

    def ready(self):
        from . import signals  # NOQA
