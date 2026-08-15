-- NeonTiers Tournament Bot -- Migráció az új queue/ticket UI-hoz.
-- Futtasd le a Supabase SQL Editorban. Minden oszlop IF NOT EXISTS-szel jön
-- létre, tehát ártalmatlan, ha egy részük már létezik.

alter table tournaments
    add column if not exists ft integer not null default 1;

alter table tournaments
    add column if not exists posted_at bigint;

-- meglévő sorokhoz (ha még nincs posted_at) állítsunk be egy ésszerű
-- alapértéket, hogy a "Létrehozva" sor ne törjön el a régi bajnokságoknál.
update tournaments set posted_at = extract(epoch from now())::bigint
where posted_at is null;

alter table matches
    add column if not exists ticket_message_id bigint default 0;

alter table matches
    add column if not exists deadline bigint;

alter table matches
    add column if not exists score1 integer;

alter table matches
    add column if not exists score2 integer;

alter table matches
    add column if not exists ff boolean not null default false;
