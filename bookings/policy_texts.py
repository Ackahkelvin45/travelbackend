"""
Customer policy texts — v1.0, approved by the owner on 28 September 2026 from
the "Michael Blackson Ghana Experience — Customer policies and website
implementation guide" (v1.1, 22 Sep 2026), with the decisions taken since:
deposit per traveller, balance due 5 Dec 2026, hotels per person, 50% retained
on a cancelled experience or hotel, additions until 7 days before departure,
name changes handled free of charge by the team, farewell dinner included.

Published documents are immutable: to change wording, bump VERSION and add
the new text — never edit a published version (see PolicyDocument.save).
"""

VERSION = "1.0"

OPERATOR = "Azura Travels"
SUPPORT_EMAIL = "hello@azuratravels.live"
SUPPORT_PHONES = "+233 24 123 4567 · +233 20 987 6543"
ADDRESS = "14 Adeola Odeku Street, Accra, Ghana"
TOUR = "Michael Blackson Ghana Experience, 4–11 January 2027"

TERMS = f"""BOOKING TERMS AND CONDITIONS
{OPERATOR} — {TOUR}
Version {VERSION}

These terms, together with the Refund Policy and the Installment Policy (which form part of these terms), govern every booking made with {OPERATOR}. The separate Privacy Policy explains how we handle your personal information.

1. YOUR BOOKING
Every traveller books the Core Tour. Optional Accra accommodation and optional experiences are included only when selected and confirmed. Your checkout summary and confirmation identify the tour dates, travellers, inclusions, total price and the policy versions you accepted. A booking is confirmed only after verified payment and available places.

2. NAME CORRECTIONS AND BOOKING TRANSFERS
Contact us promptly if a traveller's name is incorrect. Corrections are free of charge. A transfer to a replacement traveller may be requested up to 14 calendar days before departure, subject to availability, supplier approval and the replacement traveller accepting these terms; requests after that date are reviewed individually and are not guaranteed. Transfers and corrections are made by our team at no charge. We will tell you before you agree if a supplier charge applies; no automatic administration fee applies.

3. OPTIONAL EXPERIENCES AND LATER ADDITIONS
Only the options shown on your confirmation are included. Options are subject to capacity, activity suitability and venue rules. You may add an experience after booking, up to 7 days before departure; it is priced at its listed price, is payable before it is confirmed, and joins your booking once paid. Existing items are never repriced.
If you cancel a confirmed experience or hotel option, 50% of that item's price is retained and the remaining 50% is credited to your booking — refunded if you have already paid more than the new total, otherwise deducted from your outstanding balance. Cancelling an option does not affect any bundle discount already applied. If we cancel an option and no acceptable replacement is agreed, we refund the amount paid for that option in full.

4. HOTEL AND ROOM POLICY
Your confirmation identifies the accommodation category, room basis, check-in and checkout dates and included meals. Accra hotel options are priced per person and cover six nights with breakfast; one night at Crown Forest (Standard Room) is included in the Core Tour for everyone. Choosing No Accra Hotel means you arrange your own Accra accommodation and reach the published meeting points at your own expense. Early check-in, late checkout, extra nights, room upgrades, incidentals and damage charges are excluded unless expressly listed.

5. ITINERARY AND ORGANIZER CHANGES
We may adjust timings, routes and comparable venues for weather, safety or operational reasons and will notify you of significant changes. Proposed venues, visits and personal appearances are not guaranteed unless expressly confirmed. If a material advertised feature, including a specifically promised Michael Blackson appearance, cannot be delivered, we will explain the change and offer an appropriate alternative, price reduction or refund option according to the affected service and applicable law.
If we cancel the entire tour, we will offer a full refund of tour payments received, or an alternative you freely accept. Credits or rescheduling are optional for you. Independently purchased flights and other outside arrangements are not automatically reimbursed, subject to applicable rights. The guest cancellation percentages in the Refund Policy never apply to a cancellation by us.

6. FORCE MAJEURE
Events beyond reasonable control, such as severe weather, disaster, public restrictions or serious unrest, may prevent or disrupt services. We will notify you promptly, explain the affected services and offer feasible alternatives. If services cannot be delivered, refunds or other remedies will reflect the undelivered services and applicable law. We will not automatically forfeit all payments or require you to accept a credit.

7. CONDUCT AND REMOVAL
Guests must follow lawful instructions, venue rules and reasonable safety directions. Harassment, threats, violence, illegal drugs, serious intoxication or behaviour that endangers others may result in refused participation or removal. Where safe, staff will explain the concern and allow correction; immediate action may be necessary for serious risk. Any refund decision will consider the circumstances and your applicable rights.

8. HEALTH, ACCESSIBILITY AND EMERGENCY SUPPORT
Please assess whether the activities suit your health and mobility, obtain professional travel-health advice, and tell us about relevant access, dietary or support requirements before booking so we can discuss reasonable arrangements and confirm what can be provided. Not every venue or activity can be guaranteed accessible. Waterfall, swimming, safari and massage participation may depend on local safety conditions and provider screening.
Bring any medication you need and your emergency contacts, and follow safety briefings. Staff may stop an unsafe activity or seek emergency help. Emergency medical and evacuation expenses may be your responsibility, subject to your insurance and applicable law.

9. TRAVEL DOCUMENTS AND ARRIVAL
You are responsible for checking passport, visa, transit and health-entry requirements for your nationality with the relevant authorities (see the official Ghana Immigration Service guidance), and for arranging travel in time to join the tour. The tour does not include a Visa on Arrival service and cannot guarantee entry. Denied boarding, visa refusal and personal flight delays follow the Refund Policy unless mandatory law requires otherwise. Tell us promptly about delays so available arrangements can be discussed.

10. INSURANCE AND LIABILITY
We strongly recommend suitable travel insurance from the time you book, covering cancellation, medical care, evacuation, baggage and your planned activities. Insurance is not included unless stated; please check exclusions and coverage yourself.
We will exercise reasonable care in organizing the experience. You remain responsible for your conduct, personal belongings and independent arrangements. Nothing in these terms excludes responsibility or remedies that cannot lawfully be excluded, including liability where applicable for negligence, fraud or statutory consumer rights.

11. PAYMENT, DEADLINES AND CANCELLATION
Deposits, top-up payments, the final payment deadline and overdue balances are governed by the Installment Policy. Cancellations and refunds are governed by the Refund Policy. Both form part of these terms.

12. PAYMENT DISPUTES AND CHARGEBACKS
If you do not recognise a charge or believe there is an error, contact us so we can investigate. This does not restrict your right to contact your bank or exercise lawful dispute rights. We will provide accurate booking and payment records in response to disputes and will not charge a punitive dispute fee. While a bank dispute on a payment is open, we do not separately refund that payment.

13. ACCEPTANCE AND RECORDS
Before payment you confirm that you have read and agree to these Booking Terms, including the installment deadlines and the cancellation and refund policy, that the traveller information is accurate, and that you are authorised to book for the travellers listed. We record the version of each policy you accepted, the time of acceptance and your booking reference, and send you a durable copy with your confirmation. Later changes to these pages do not alter an existing booking. Material changes to your booking require your explicit agreement.

14. COMPLAINTS
Contact us at {SUPPORT_EMAIL} or {SUPPORT_PHONES} ({ADDRESS}). We aim to acknowledge complaints within two business days and to give a substantive response within ten business days, or an update explaining any delay.
"""

REFUND = f"""REFUND POLICY
{OPERATOR} — {TOUR}
Version {VERSION}

This policy applies when you cancel your booking. Cancellations by {OPERATOR} are covered by the Booking Terms (organizer changes and force majeure) and always carry a full refund of tour payments received.

1. CANCELLATION SCHEDULE
Notice received before departure on 4 January 2027 — refund of the amount paid:
• 60 or more calendar days before departure: 90% (on or before 5 November 2026)
• 30 to 59 calendar days before departure: 60% (6 November to 5 December 2026)
• 14 to 29 calendar days before departure: 40% (6 December to 21 December 2026)
• Fewer than 14 calendar days before departure: no refund (22 December 2026 onward)

2. ARRIVAL RULE
Once you have arrived in Ghana or the experience has started for you, whichever happens first, voluntary cancellation, non-participation, early departure and unused activities receive no refund. This does not remove your remedies where we cancel, fail to deliver a service, or where rights cannot legally be excluded.

3. HOW THE REFUND IS CALCULATED
The refund percentage is applied to the money actually received for your booking, including any deposit and top-up payments, less any refunds already made. The percentage is never applied to an unpaid balance, the deposit is never treated as non-refundable, and no second cancellation penalty is added. Once you cancel, no further balance becomes due.
Non-refundable third-party items shown as such on your confirmation are excluded before the percentage is applied.

4. WHEN YOUR CANCELLATION COUNTS
Your cancellation counts from the moment we receive it through your booking dashboard or our published support email — not from when we process it. The date is judged in Ghana time (GMT). For example, a request received on 5 December qualifies for 60% throughout that day, unless the arrival rule applies. We send an immediate dated acknowledgment and pause further payment collection while we review the request.

5. CANCELLING PART OF A BOOKING
You may cancel a single optional experience or hotel option without cancelling the tour: 50% of that item's price is retained and the remaining 50% is credited to your booking, refunded where you have already paid more than the new total. Cancelling an option never removes a discount already applied to the items you keep.

6. PROCESSING
We aim to review cancellation requests within five business days and to initiate approved refunds within ten business days of approval. Refunds go to your original payment method wherever possible, across the original transactions as required. We will send you the refunded amount, the initiation date and a reference; banks and card providers post refunds at different speeds, and we cannot promise a bank receipt date. No undisclosed administration or processing deductions are made. Refunds to closed cards or failed refunds are handled with verified payer identity through the payment provider's supported process. While a bank dispute on a payment is open, that payment is not separately refunded.
"""

INSTALLMENT = f"""INSTALLMENT POLICY
{OPERATOR} — {TOUR}
Version {VERSION}

You may pay in full, or secure your booking with the initial payment shown at checkout and pay the rest later. You may make additional payments at any time before your final payment deadline. Every successful payment reduces your outstanding balance. Your confirmation and your booking dashboard show the total price, the amount paid, the remaining balance and the final due date. Flexible payments do not extend that deadline.

1. INITIAL PAYMENT
The minimum initial payment is US$1,000 per traveller, capped at that traveller's package price — a package below US$1,000 is simply paid in full, and you are never charged more than your booking total. For a group, the initial payment is the sum of each traveller's capped amount. At checkout you may pay any amount from the minimum initial payment up to the full balance.

2. TOP-UP PAYMENTS
After the initial payment, top-ups may be any amount up to the outstanding balance. There are no fixed monthly installments, no interest and no automatic debits.

3. FINAL PAYMENT DEADLINE
Full payment is due 30 calendar days before departure: for the 4 January 2027 departure, that is 5 December 2026 at 23:59 Ghana time (GMT). Bookings made on or after that date are payable in full at checkout, subject to availability. We send reminders 14, 7 and 1 day before the deadline while a balance remains.

4. FAILED AND OVERDUE PAYMENTS
A failed payment does not confirm a new booking or increase the amount paid; you will see a clear message and can safely try again. A failed top-up before the deadline does not cancel a booking whose initial payment succeeded.
If your balance is still outstanding after the deadline, we send an overdue notice and give you 72 hours to settle it. No late fee applies and nothing is cancelled automatically. If the balance remains unpaid after that period, our team will contact you before any change is made; a booking may then be cancelled after notice, with the Refund Policy applied as at the actual cancellation date. Any agreed extension is recorded in writing with a new due date.

5. YOUR PLACE ON THE TOUR
Places are held for a limited checkout window; an unpaid booking lapses after 24 hours. A booking is confirmed only after verified successful payment and confirmed availability. Every successful payment receives a receipt showing your booking reference and remaining balance.

Questions: {SUPPORT_EMAIL} · {SUPPORT_PHONES}
"""

PRIVACY = f"""PRIVACY POLICY
{OPERATOR}
Version {VERSION}

1. WHO WE ARE
{OPERATOR}, {ADDRESS}. Privacy enquiries: {SUPPORT_EMAIL}.

2. WHAT WE USE YOUR INFORMATION FOR
We use your contact, booking, traveller and payment-reference details to manage reservations, collect payments, provide services, communicate changes and handle support or legal obligations. Relevant information is shared with our payment provider and with the hotels, transport operators and activity providers needed to deliver your booking. We do not receive or store your complete card details — payments are processed by Paystack on secure, tokenised pages. Service messages about your booking are not permission for unrelated marketing.

3. WHAT WE COLLECT
Name, email address, phone number and country (required to make a booking); special requests you choose to share, including dietary, accessibility or medical needs (optional, handled with restricted access and never repeated in general booking emails); payment references and amounts; the policy versions you accepted and when; and account details if you create one.

4. HOW LONG WE KEEP IT
Booking and payment records are kept for as long as required for accounting, tax and dispute purposes after the tour. Account details are kept until you ask us to delete your account. Where we must keep a record for legal reasons, we tell you so when you ask for deletion.

5. YOUR RIGHTS
You may ask to access, correct or delete your personal information, object to certain processing, or withdraw consent you have given, by contacting {SUPPORT_EMAIL} or using your booking dashboard. We explain any records we must retain. You may complain to the Data Protection Commission of Ghana.

6. MARKETING
Marketing emails are sent only to people who subscribe. Every marketing email contains a one-click unsubscribe link. Subscribing is separate from making a booking.

7. COOKIES
Our website uses only the cookies and browser storage needed to keep you signed in and to remember your booking in progress. We do not load advertising or analytics trackers.

8. SECURITY
We use encrypted connections, role-based access for our team, and audit trails for changes to bookings and payments. Passport documents are not requested online.

9. PHOTOGRAPHY AND MEDIA
Promotional photography or filming may take place during the experience. Whether we may use identifiable images of you is a separate, optional choice you can make at checkout or by contacting us; declining never affects your booking. See the Photography and Media Consent notice.
"""

MEDIA = f"""PHOTOGRAPHY AND MEDIA CONSENT (OPTIONAL)
{OPERATOR} — {TOUR}
Version {VERSION}

Promotional photography or filming may occur during the experience. By ticking this box you allow {OPERATOR} to use identifiable images or video of you on our website, social media and promotional materials.

This is entirely optional. Declining does not affect your booking in any way. If you prefer not to be filmed, tell the team at any point during the tour. You may withdraw this consent for future use at any time by contacting {SUPPORT_EMAIL}; we will explain any practical limits for materials already distributed. Private group photos are kept separate from promotional publication.
"""

DOCUMENTS = [
    # (type, title, body, is_required)
    ("terms", "Booking Terms and Conditions", TERMS, True),
    ("refund", "Refund Policy", REFUND, True),
    ("installment", "Installment Policy", INSTALLMENT, True),
    ("privacy", "Privacy Policy", PRIVACY, True),
    ("media", "Photography and Media Consent", MEDIA, False),
]
