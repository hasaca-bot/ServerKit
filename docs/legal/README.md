# Legal templates

**TEMPLATE — NOT LEGAL ADVICE. Must be reviewed by a qualified lawyer in Turkey/EU before use.**

These are starting drafts for the hosting and AI-managed VPS service sold as {{BRAND}} (working name; the final name is not cleared, see below). They are not published and not reviewed. Every `{{PLACEHOLDER}}` must be filled in, and every bracketed `[Lawyer: …]` note must be resolved or deleted before anything goes live.

## Index

| File | Language | Purpose |
|---|---|---|
| [terms-of-service.md](terms-of-service.md) | English | Contract terms: accounts, plans, free tier, billing, suspension, data deletion, liability, governing law |
| [privacy-policy.md](privacy-policy.md) | English | GDPR Art. 13 and KVKK information notice for the website and services |
| [acceptable-use-policy.md](acceptable-use-policy.md) | English | Prohibited content and activity, fair use and enforcement |
| [takedown-policy.md](takedown-policy.md) | English | Notice-and-takedown under Law 5651, DSA Arts. 16–17 and DMCA-style notices; counter-notice; repeat infringers |
| [kvkk-aydinlatma-metni.md](kvkk-aydinlatma-metni.md) | Turkish | KVKK Art. 10 information notice |
| [mesafeli-satis-on-bilgilendirme.md](mesafeli-satis-on-bilgilendirme.md) | Turkish | Distance-sales pre-contract information for online subscriptions, including the withdrawal right and the digital-service exception |

Related files outside this folder:

- `NOTICE` (repo root): attribution for ServerKit, contributors, IBM Plex (OFL), Font Awesome (CC BY 4.0) and Simple Icons trademarks.
- `scripts/gen-third-party-notices.sh`: generates `THIRD-PARTY-NOTICES.md` from the backend venv (`pip-licenses`) and frontend production dependencies (`license-checker-rseidelsohn`). Run from the repo root; set `VENV` to choose the Python environment.
- `docs/HOSTING-ROADMAP.md` section 6: the legal checklist these templates follow.

## Placeholders

Common placeholders used across files: `{{BRAND}}`, `{{COMPANY_NAME}}`, `{{COMPANY_ADDRESS}}`, `{{MERSIS_NO}}`, `{{TAX_OFFICE}}`, `{{TAX_NO}}`, `{{CONTACT_EMAIL}}`, `{{KEP_ADDRESS}}`, `{{EFFECTIVE_DATE}}`, `{{WEBSITE_URL}}`, `{{PRICING_URL}}`, `{{HOSTING_PROVIDER}}`, `{{DATA_CENTER_REGION}}`, `{{PAYMENT_PROCESSOR}}`, `{{EMAIL_PROVIDER}}`, `{{AI_PROVIDER}}`, `{{DELETION_DAYS}}`, `{{TAKEDOWN_EMAIL}}`, `{{ABUSE_EMAIL}}`.

Free-tier and pricing numbers belong on the pricing page, not in these templates. Use the roadmap example figures only as placeholders.

## Open questions for the lawyer

1. **Cloudflare reseller terms.** Self-serve Cloudflare terms (§2.2.1(a)) prohibit reselling access to third parties. Confirm whether an Enterprise or reseller agreement is needed, or whether customer traffic must go through infrastructure we own (own reverse proxy and Let's Encrypt), with Cloudflare used only for our own domains. The Privacy Policy and KVKK notice list Cloudflare as a processor and must be updated to match the answer.
2. **Law 5651 log retention.** Confirm the traffic-log retention period that applies to a hosting provider (“yer sağlayıcı”) and whether it differs from the access-provider period in the secondary regulation. Also confirm the response deadlines for access-ban orders in Arts. 8–9.
3. **KDV on electronic services.** Confirm VAT treatment of SaaS and hosting sold to Turkish consumers and businesses, and whether an ETBİS registration and any other e-commerce obligations apply.
4. **EU OSS VAT.** Confirm whether EU consumer sales require OSS registration, which member state is the registration point, and how the reverse-charge rules apply to EU business customers.
5. **Withdrawal-right exception.** Confirm that the digital-service exception in the distance-sales form applies to subscriptions, what consent and acknowledgement wording is needed, and whether auto-renewing plans need separate subscription disclosures under Turkish or EU rules.
6. **VERBIS.** Confirm whether registration is required under KVKK Art. 16 and the Board’s thresholds, and record the status in the Privacy Policy.
7. **International transfers.** Confirm the KVKK Art. 9 mechanism for each processor after the 2024 amendment, and the EU SCC or Data Privacy Framework basis for transfers from the EEA.
8. **DSA and DMCA scope.** Confirm whether the service is an EU “hosting service” that must publish transparency reports, and whether DMCA safe-harbour procedures are needed for US rights holders.
9. **Liability cap and consumer terms.** Confirm that the cap and the free-tier limits do not conflict with TBK Art. 115 or the unfair-terms rules in Law 6502 Art. 5 and Directive 93/13/EEC.
10. **Governing law.** Confirm Turkish law and the Turkish courts are acceptable for business customers, and that EU consumers keep their home-country protection.
11. **Contributor consent if ever relicensing.** The roadmap says the code stays MIT. A closed or dual-licensed edition would need consent from every contributor, and no CLA exists. Confirm the approach before any change.
12. **Name clearance.** “ServerKit” must not be used for the service. Run TÜRKPATENT, EUIPO and USPTO clearance for the final {{BRAND}} name before launch.
13. **App catalogue licences.** Confirm the hosted catalogue exclusions (SSPL, BSL, Sustainable Use and similar licences) and the rules for AGPL images.
