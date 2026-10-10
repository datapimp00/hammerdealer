-- supabase/migrations/003_seed_listings.sql
-- 2HEART FINAL ADMIN SECURE 2FA — seed demo listings across the 6 portals so the UI
-- has data before the first cron sync. Idempotent (on conflict do nothing).
insert into public.listings (id, title, price, city, url, image, portal, active) values
  ('wg-ges-001', 'Helles WG-Zimmer in Berlin-Kreuzberg', 620, 'Berlin', 'https://www.wg-gesucht.de/', 'https://i.pravatar.cc/150?img=12', 'wg-gesucht', true),
  ('wg-ges-002', '2er WG nahe TU Berlin', 540, 'Berlin', 'https://www.wg-gesucht.de/', 'https://i.pravatar.cc/150?img=32', 'wg-gesucht', true),
  ('klein-001', 'Möblierte 1-Zimmer-Wohnung Prenzlauer Berg', 890, 'Berlin', 'https://www.kleinanzeigen.de/', 'https://picsum.photos/300/200?random=11', 'kleinanzeigen', true),
  ('klein-002', 'Studio mit Balkon Mitte', 760, 'Berlin', 'https://www.kleinanzeigen.de/', 'https://picsum.photos/300/200?random=12', 'kleinanzeigen', true),
  ('immo-001', 'Neubau-Wohnung mit Einbauküche', 1240, 'Berlin', 'https://www.immobilienscout24.de/', 'https://i.pravatar.cc/150?img=5', 'immoscout', true),
  ('immo-002', 'Altbauwohnung Friedrichshain', 980, 'Berlin', 'https://www.immobilienscout24.de/', 'https://i.pravatar.cc/150?img=8', 'immoscout', true),
  ('face-001', 'Marketplace WG-Zimmer Neukölln', 480, 'Berlin', 'https://www.facebook.com/marketplace/', 'https://picsum.photos/300/200?random=21', 'facebook', true),
  ('face-002', 'Günstiges Zimmer Wedding', 430, 'Berlin', 'https://www.facebook.com/marketplace/', 'https://picsum.photos/300/200?random=22', 'facebook', true),
  ('whats-001', 'WhatsApp-Direkt: Zimmer Charlottenburg', 710, 'Berlin', 'https://wa.me/', 'https://i.pravatar.cc/150?img=45', 'whatsapp', true),
  ('ebay-001', 'Kleinanzeigen-Fund via eBay-Aggregator', 690, 'Berlin', 'https://www.ebay-kleinanzeigen.de/', 'https://picsum.photos/300/200?random=31', 'ebay', true)
on conflict (id) do nothing;
