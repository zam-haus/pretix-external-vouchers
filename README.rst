External Vouchers
=================

This is a plugin for `pretix`_ that lets customers pay with vouchers managed outside of pretix.

Once the plugin is enabled for an event, it does the following:

* It adds a payment method **External voucher**, based on pretix' manual payment. During checkout it asks the
  customer a configurable question (e.g. "Please enter your voucher code") and stores the answer on the payment
  (visible in the backend order view, in the REST API as ``details.answer`` and searchable as matching ID).
* For orders using this payment method, the payment deadline is extended until the end of the event (for event
  series: the end of the latest date in the order; if no end is set, the start) and the order is marked as
  *valid if pending*, so tickets can be downloaded and checked in before the payment is confirmed. The payment
  stays pending until it is confirmed manually.
* It hides pretix' built-in "Redeem a voucher" (Gutschein einlösen) box on the event page and in the cart. Built-in
  vouchers keep working through voucher links (``/redeem?voucher=CODE``).

Configure the payment method under *Event settings → Payment → External voucher*.

Known limitation: the JavaScript embed widget renders its own voucher field, which is not hidden.

Development setup
-----------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``pip install -e .`` within this directory to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.

This plugin has CI set up to enforce a few code style rules. To check locally, you need these packages installed::

    pip install flake8 isort black

To check your plugin for rule violations, run::

    black --check .
    isort -c .
    flake8 .

You can auto-fix some of these issues by running::

    isort .
    black .

To automatically check for these issues before you commit, you can run ``.install-hooks``.


License
-------


Copyright 2026 Julian Hammer

Released under the terms of the Apache License 2.0



.. _pretix: https://github.com/pretix/pretix
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
