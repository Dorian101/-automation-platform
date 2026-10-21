-- Where a visit came from, when the browser said so.
--
-- `referrer` holds the origin of the page that led here — `https://www.google.com`,
-- never the full URL. The browser sends the header only on a real link, and
-- modern browsers already shorten it to the origin when crossing sites, so a
-- path or a search query is dropped before it reaches here; this column stores
-- the origin and nothing more. A direct visit, a bookmark, or a browser that
-- withholds the header leaves it NULL, which reads as "no referrer" rather
-- than as an empty string that looks like one.
--
-- The reason it stops at the origin is the same reason the table stores no
-- address: a full referrer from a search engine would carry what somebody
-- typed into it, and a log of visits is not a log of queries. The origin
-- answers "which site led here" without collecting the question.

ALTER TABLE visits ADD COLUMN referrer TEXT;
