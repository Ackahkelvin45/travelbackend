"""Manual debug helper — run directly with `python test_verify.py`.

Named test_*.py so unittest discovery used to import it and hit the LIVE
Paystack API during `manage.py test`; everything now lives under a
__main__ guard so the suite never touches the network.
"""

if __name__ == "__main__":
    import os
    import django

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()

    from payments.models import Payment
    from payments.paystack import verify_transaction

    payment = Payment.objects.last()
    if not payment:
        print("No payments found in DB")
    else:
        print(f"Latest payment ref: {payment.paystack_reference}")
        print(f"Current DB status: {payment.status}")
        try:
            data = verify_transaction(payment.paystack_reference)
            print("Paystack response status:", data.get('status'))
            print("Paystack response amount:", data.get('amount'))
        except Exception as e:
            print("Error verifying with Paystack:", e)
