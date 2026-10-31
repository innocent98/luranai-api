# Offerings catalog: FE integration guide, part 1 (backend core)

**Status:** backend core (stream S1) built and exercised live on 2026-10-31. Part 2 (chat messages carrying `meta.offerings`, the catalog quote, item cards) is written by the AI-path stream after its own live run; nothing in part 2 is described here.

**Every request and response body below is copied from a capture of the running app** (API + arq worker + Postgres 15 + Redis), stored in `docs/fe-integration-captures/offerings/` (file names are cited under each block). Re-derive any claim with `scripts/capture_offerings.py` (instructions in its header). Placeholders: ids and timestamps are from a throwaway database; tokens are replaced by `<jwt-placeholder>`; tenants and e-mails are fake (`example.com`).

**What was NOT real in the live run** (so you know how far to trust the photo sections): the Cloudinary uploader and the image moderation were stand-ins (`tests/fakes/live_stub.py`). The photo code, validation, cap, ordering and error handling were real; the URLs, `width`, `height` and `bytes` in photo responses are made up by the stand-in. The LLM was never called (not used by this part). A real Cloudinary and a real moderation call have not been exercised.

## 1. What the feature is

The owner keeps a list of what the business sells today (products and services): name, description, photos, a price (fixed, "from", or on request), and an availability status. The AI answers "what do you have, how much, is it available" from this list on every channel (part 2). The owner updates it daily from a phone.

This part is the REST API the dashboard builds against: items, photos, bulk import, the daily "everything is still correct" confirm, settings, the Not-found list (what customers asked for that is not listed), the plan meter, and the wanted items shown on a lead.

## 2. Conventions

| | |
|---|---|
| Base path | `/api/v1/offerings` |
| Auth | The staff session token as everywhere else (`Authorization: Bearer <jwt>`). **Reads: any staff role. Writes: tenant admin only.** |
| Success envelope | `{ "data": ... }` (lists add `"meta"`). `DELETE` answers `204` with no body. |
| Money | **Whole naira as an integer** (`12000` is ₦12,000). Never kobo, never a decimal. Format it yourself; the API sends no formatted price in part 1. |
| Item reference | `seq` is a per-business number, never reused, shown to the owner as `ref` (`"#12"`). |
| Timestamps | ISO 8601 with offset (`2026-10-30T23:07:03.897971+00:00`). |
| Lapsed account | Reads work. Every write answers `403 feature_not_in_plan` (see section 6). |

### Four error shapes (the FE must handle all four)

1. **Module errors** (flat): `{ "code": "...", "message": "..." }` plus extra keys for some codes. Used by every business-rule error of this feature.
2. **Validation of the item or settings fields**: `{ "code": "validation", "message": "Some fields are not valid.", "errors": [ { "field": "price", "code": "price_required" } ] }`. `errors[]` carries a `code` per field and **no message text**; write your own copy per code (table in section 12).
3. **The framework's own `422`** for a wrong *type* or an out-of-range *query parameter* (for example `kind: "gadget"`, `price: "abc"`, `?days=91`): `{ "detail": [ { "type": ..., "loc": [...], "msg": ..., "input": ... } ] }`. Different shape from shape 2. See captures `13-create-wrong-type-fastapi-shape`, `66-misses-window-out-of-range`.
4. **Auth and plan errors that are not module errors**: `401` and role `403` use `{ "detail": "..." }`; the lapsed/plan `403` uses the entitlements body `{ code, feature, min_tier, message }`; the downgrade refusal is `400 { "detail": { "error": "data_exceeds_limits", ... } }`.

Auth failures (captures `01`, `02`):

`401` (capture `01-list-unauthenticated.json`)

```json
{
  "detail": "Could not validate credentials"
}
```
`403` for a support agent trying to write (capture `02-create-as-support-agent.json`)

```json
{
  "detail": "Not enough permissions. Tenant admin access required."
}
```

### Field-nesting traps

- The item is `data` on single-item endpoints and `data[]` on the list. The plan meter is under `meta` on the list (`meta.limit`, `meta.used`) and under `features.offerings` on `GET /api/v1/tenant/entitlements` (`limit`, `used`); they are the same numbers.
- **`meta.limit` is the plan's item cap, not the page size.** The page size is `meta.page_size`. (`limit` the *query parameter* is the page size.) `meta.limit` is `null` on an unlimited plan.
- `price` is `null` (present, not absent) for `on_request` items. Optional text fields are present as `null`, not absent.
- `cover_url` is `null` when there are no photos; otherwise it is the **400 px thumbnail** of the cover, not the stored original. Use `images[].url` for the stored copy.
- The lead detail endpoint keeps its own older envelope: `{ "success": true, "data": { ... } }`. Everything else in this feature uses `{ "data": ... }` without `success`.
- A photo upload answers with the **photo** under `data`; a photo reorder answers with the whole **item** under `data`.

## 3. The plan meter and the caps

Offerings is on every plan. Item cap (active items only): Trial 25, Lite 100, Professional 500, Growth 2,000, Enterprise unlimited. Photos per item: Trial 4, Lite 4, Professional 8, Growth 8, Enterprise 8. These are launch values and may change. Read the item cap from the API (`limit` below); the photo limit is only returned inside an upload refusal (see the note after the example).

Meter on the entitlements endpoint, `offerings` entry only (capture `57-entitlements-at-the-cap.json`; the full response also lists every other feature):

```json
{
  "offerings": {
    "enabled": true,
    "min_tier": "trial",
    "limit": 25,
    "used": 25
  }
}
```

The per-item photo limit is **not** on this endpoint. It is returned when an upload is refused (`offering_image_limit`, `limit`) and is the plan's `max_offering_images` (4 or 8 above). To show "2 of 4 photos" before the user tries, count `photo_count` and use the plan table above as a fallback until a dedicated field exists (see follow-ups).

## 4. Items

### 4.1 Create

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "Canvas sneaker",
  "price_mode": "fixed",
  "price": 12000,
  "availability": "in_stock",
  "description": "Black, sizes 40-45",
  "category": "Shoes",
  "keywords": "trainers, sneakers",
  "negotiable": true
}
```

Response `201` (capture `06-create-product.json`)

```json
{
  "data": {
    "id": "215a6240-fc7a-4286-a0d4-11a0a27fbc87",
    "seq": 1,
    "ref": "#1",
    "kind": "product",
    "name": "Canvas sneaker",
    "description": "Black, sizes 40-45",
    "category": "Shoes",
    "keywords": "trainers, sneakers",
    "price_mode": "fixed",
    "price": 12000,
    "price_note": null,
    "availability": "in_stock",
    "quantity": null,
    "negotiable": true,
    "status": "active",
    "external_ref": null,
    "images": [],
    "photo_count": 0,
    "cover_url": null,
    "availability_changed_at": "2026-10-30T23:07:03.897971+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.897971+00:00",
    "updated_at": "2026-10-30T23:07:03.897971+00:00"
  }
}
```

Notes: `availability_changed_at` is when the availability last changed. `negotiable: true` means "owner open to offers". `kind` is `product` or `service`; availability labels differ by kind in the UI (services: Available / Limited slots / Fully booked / By request) but the values are the same four: `in_stock`, `limited`, `out_of_stock`, `on_request`.

A service with a "from" price, a note and limited availability:

`POST /api/v1/offerings`

```json
{
  "kind": "service",
  "name": "Haircut",
  "price_mode": "from",
  "price": 3000,
  "availability": "limited",
  "price_note": "per session",
  "quantity": 4,
  "category": "Services"
}
```

Response `201` (capture `07-create-service-from-price.json`)

```json
{
  "data": {
    "id": "e9d962d4-5126-469e-b7ea-ff35b1ac61cf",
    "seq": 2,
    "ref": "#2",
    "kind": "service",
    "name": "Haircut",
    "description": null,
    "category": "Services",
    "keywords": null,
    "price_mode": "from",
    "price": 3000,
    "price_note": "per session",
    "availability": "limited",
    "quantity": 4,
    "negotiable": false,
    "status": "active",
    "external_ref": null,
    "images": [],
    "photo_count": 0,
    "cover_url": null,
    "availability_changed_at": "2026-10-30T23:07:03.926064+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.926064+00:00",
    "updated_at": "2026-10-30T23:07:03.926064+00:00"
  }
}
```

An `on_request` item takes no price:

`POST /api/v1/offerings`

```json
{
  "kind": "service",
  "name": "Wedding styling",
  "price_mode": "on_request",
  "price": null,
  "availability": "on_request"
}
```

Response `201` (capture `08-create-on-request.json`)

```json
{
  "data": {
    "id": "d06dcb9e-269b-4870-96f7-a4159af71b23",
    "seq": 3,
    "ref": "#3",
    "kind": "service",
    "name": "Wedding styling",
    "description": null,
    "category": null,
    "keywords": null,
    "price_mode": "on_request",
    "price": null,
    "price_note": null,
    "availability": "on_request",
    "quantity": null,
    "negotiable": false,
    "status": "active",
    "external_ref": null,
    "images": [],
    "photo_count": 0,
    "cover_url": null,
    "availability_changed_at": "2026-10-30T23:07:03.945451+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.945451+00:00",
    "updated_at": "2026-10-30T23:07:03.945451+00:00"
  }
}
```

### 4.2 Validation errors

A fixed or from item without a price:

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "Canvas sneaker",
  "price_mode": "fixed",
  "price": null,
  "availability": "in_stock"
}
```

Response `422` (capture `09-create-price-required.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "price",
      "code": "price_required"
    }
  ]
}
```

An on_request item with a price:

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "Canvas sneaker",
  "price_mode": "on_request",
  "price": 500,
  "availability": "in_stock"
}
```

Response `422` (capture `10-create-price-not-allowed.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "price",
      "code": "price_not_allowed"
    }
  ]
}
```

Price must be above 0 and at most 10,000,000,000:

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "Canvas sneaker",
  "price_mode": "fixed",
  "price": 0,
  "availability": "in_stock"
}
```

Response `422` (capture `11-create-price-out-of-range.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "price",
      "code": "price_out_of_range"
    }
  ]
}
```

All violations come back together:

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "  ",
  "price_mode": "fixed",
  "price": null,
  "availability": "in_stock"
}
```

Response `422` (capture `12-create-several-errors.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "name",
      "code": "name_required"
    },
    {
      "field": "price",
      "code": "price_required"
    }
  ]
}
```

The framework shape for a wrong type (shape 3 above):

`POST /api/v1/offerings`

```json
{
  "kind": "gadget",
  "name": "Canvas sneaker",
  "price_mode": "fixed",
  "price": "abc",
  "availability": "in_stock"
}
```

Response `422` (capture `13-create-wrong-type-fastapi-shape.json`)

```json
{
  "detail": [
    {
      "type": "literal_error",
      "loc": [
        "body",
        "kind"
      ],
      "msg": "Input should be 'product' or 'service'",
      "input": "gadget",
      "ctx": {
        "expected": "'product' or 'service'"
      }
    },
    {
      "type": "int_parsing",
      "loc": [
        "body",
        "price"
      ],
      "msg": "Input should be a valid integer, unable to parse string as an integer",
      "input": "abc"
    }
  ]
}
```

### 4.3 List, filter, page

`GET /api/v1/offerings` query parameters: `status` (`active` default, or `archived`), `kind`, `category` (exact, case-insensitive), `availability`, `q` (searches name, keywords and category, case-insensitive; `%` and `_` are plain text), `limit` (1 to 100, default 50), `cursor`. Newest first. The first element of `data` is shown; the others have the same shape.

`GET /api/v1/offerings`

Response `200` (capture `30-list-default.json`)

```json
{
  "data": [
    {
      "id": "d06dcb9e-269b-4870-96f7-a4159af71b23",
      "seq": 3,
      "ref": "#3",
      "kind": "service",
      "name": "Wedding styling",
      "description": null,
      "category": null,
      "keywords": null,
      "price_mode": "on_request",
      "price": null,
      "price_note": null,
      "availability": "on_request",
      "quantity": null,
      "negotiable": false,
      "status": "active",
      "external_ref": null,
      "images": [],
      "photo_count": 0,
      "cover_url": null,
      "availability_changed_at": "2026-10-30T23:07:03.945451+00:00",
      "archived_at": null,
      "created_at": "2026-10-30T23:07:03.945451+00:00",
      "updated_at": "2026-10-30T23:07:03.945451+00:00"
    }
  ],
  "meta": {
    "next_cursor": null,
    "total": 3,
    "page_size": 50,
    "limit": 25,
    "used": 3
  },
  "_trimmed_by_the_guide": "2 more items of the same shape omitted"
}
```
Filter by kind and search text:

`GET /api/v1/offerings?kind=service&q=hair`

Response `200` (capture `31-list-filter-kind-and-search.json`)

```json
{
  "data": [
    {
      "id": "e9d962d4-5126-469e-b7ea-ff35b1ac61cf",
      "seq": 2,
      "ref": "#2",
      "kind": "service",
      "name": "Haircut",
      "description": null,
      "category": "Services",
      "keywords": null,
      "price_mode": "from",
      "price": 3000,
      "price_note": "per session",
      "availability": "limited",
      "quantity": 4,
      "negotiable": false,
      "status": "active",
      "external_ref": null,
      "images": [],
      "photo_count": 0,
      "cover_url": null,
      "availability_changed_at": "2026-10-30T23:07:03.926064+00:00",
      "archived_at": null,
      "created_at": "2026-10-30T23:07:03.926064+00:00",
      "updated_at": "2026-10-30T23:07:03.926064+00:00"
    }
  ],
  "meta": {
    "next_cursor": null,
    "total": 1,
    "page_size": 50,
    "limit": 25,
    "used": 3
  }
}
```
Paging: pass `meta.next_cursor` back as `cursor`; `null` means the last page. `meta.total` is the count matching the filters.

`GET /api/v1/offerings?limit=1`

Response `200` (capture `32-list-page-1-limit-1.json`)

```json
{
  "data": [
    {
      "seq": 3,
      "_": "item trimmed by the guide"
    }
  ],
  "meta": {
    "next_cursor": "3",
    "total": 3,
    "page_size": 1,
    "limit": 25,
    "used": 3
  }
}
```
`GET /api/v1/offerings?limit=1&cursor=3`

Response `200` (capture `33-list-page-2-with-cursor.json`)

```json
{
  "data": [
    {
      "seq": 2,
      "_": "item trimmed by the guide"
    }
  ],
  "meta": {
    "next_cursor": "2",
    "total": 3,
    "page_size": 1,
    "limit": 25,
    "used": 3
  }
}
```
A cursor that is not one of ours:

`GET /api/v1/offerings?cursor=nope`

Response `422` (capture `34-list-bad-cursor.json`)

```json
{
  "code": "invalid_cursor",
  "message": "The cursor is not valid."
}
```

### 4.4 One item, change it, one-tap sold out

`GET /api/v1/offerings/{id}` returns `{ "data": <item> }` (same item shape as create, with `images` filled in, see 5.1). Unknown id or another business's id:

`GET /api/v1/offerings/00000000-0000-0000-0000-000000000000`

Response `404` (capture `36-get-not-found.json`)

```json
{
  "code": "offering_not_found",
  "message": "Offering not found"
}
```

`PATCH` takes any subset of the create fields plus `status`. **An absent field is unchanged; an explicit `null` clears an optional field** (`description`, `category`, `keywords`, `price`, `price_note`, `quantity`; `negotiable: null` means `false`). **`null` on `kind`, `price_mode`, `availability` or `name` is a `422 validation`** with `{field, code: "<field>_required"}` (`name_required` for the name) and nothing is changed. *(Added after the security review; shape taken from the automated test, not a live capture.)* The one-tap sold-out control is a patch of `availability` (note `availability_changed_at` moving):

`PATCH /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87`

```json
{
  "availability": "out_of_stock"
}
```

Response `200` (capture `37-patch-sold-out.json`)

```json
{
  "data": {
    "id": "215a6240-fc7a-4286-a0d4-11a0a27fbc87",
    "seq": 1,
    "ref": "#1",
    "kind": "product",
    "name": "Canvas sneaker",
    "description": "Black, sizes 40-45",
    "category": "Shoes",
    "keywords": "trainers, sneakers",
    "price_mode": "fixed",
    "price": 12000,
    "price_note": null,
    "availability": "out_of_stock",
    "quantity": null,
    "negotiable": true,
    "status": "active",
    "external_ref": null,
    "images": [
      {
        "id": "9103bdf1-d58f-41fb-9e30-6ef8ffc2313a",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "position": 0,
        "width": 1200,
        "height": 900,
        "bytes": 240545
      },
      {
        "id": "84cffc22-372a-4377-a5b0-2cb8eafcbe1c",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "position": 1,
        "width": 1200,
        "height": 900,
        "bytes": 240121
      },
      {
        "id": "38efe1b2-19ba-4bd8-baea-e87ddbd91c24",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "position": 2,
        "width": 1200,
        "height": 900,
        "bytes": 240015
      },
      {
        "id": "fb47e240-7447-4919-b38d-a542037cadee",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "position": 3,
        "width": 1200,
        "height": 900,
        "bytes": 240218
      }
    ],
    "photo_count": 4,
    "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
    "availability_changed_at": "2026-10-30T23:07:04.559012+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.897971+00:00",
    "updated_at": "2026-10-30T23:07:04.559012+00:00"
  }
}
```
Switching the price mode to `on_request` drops the old price (the price comes back `null`):

`PATCH /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87`

```json
{
  "price_mode": "on_request"
}
```

Response `200` (capture `38-patch-price-mode-to-on-request.json`)

```json
{
  "data": {
    "id": "215a6240-fc7a-4286-a0d4-11a0a27fbc87",
    "seq": 1,
    "ref": "#1",
    "kind": "product",
    "name": "Canvas sneaker",
    "description": "Black, sizes 40-45",
    "category": "Shoes",
    "keywords": "trainers, sneakers",
    "price_mode": "on_request",
    "price": null,
    "price_note": null,
    "availability": "out_of_stock",
    "quantity": null,
    "negotiable": true,
    "status": "active",
    "external_ref": null,
    "images": [
      {
        "id": "9103bdf1-d58f-41fb-9e30-6ef8ffc2313a",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
        "position": 0,
        "width": 1200,
        "height": 900,
        "bytes": 240545
      },
      {
        "id": "84cffc22-372a-4377-a5b0-2cb8eafcbe1c",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "position": 1,
        "width": 1200,
        "height": 900,
        "bytes": 240121
      },
      {
        "id": "38efe1b2-19ba-4bd8-baea-e87ddbd91c24",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/bf9f81d470ee4089b4e1fedc9c54430e.jpg",
        "position": 2,
        "width": 1200,
        "height": 900,
        "bytes": 240015
      },
      {
        "id": "fb47e240-7447-4919-b38d-a542037cadee",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/dec1ec0020b145c4bcef2332d6b09e12.jpg",
        "position": 3,
        "width": 1200,
        "height": 900,
        "bytes": 240218
      }
    ],
    "photo_count": 4,
    "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
    "availability_changed_at": "2026-10-30T23:07:04.559012+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.897971+00:00",
    "updated_at": "2026-10-30T23:07:04.574957+00:00"
  }
}
```
Switching back needs a price:

`PATCH /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87`

```json
{
  "price_mode": "fixed"
}
```

Response `422` (capture `39-patch-back-to-fixed-needs-a-price.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "price",
      "code": "price_required"
    }
  ]
}
```

Clearing a field with an explicit null:

`PATCH /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87`

```json
{
  "category": null
}
```

(response: `200`, item with `"category": null`; capture `41-patch-clear-category-with-null.json`)

### 4.5 Archive, restore, delete for good

`DELETE /api/v1/offerings/{id}` **archives** the item (it disappears from the AI and does not count toward the cap). Response `204`, no body (capture `43-archive-with-delete.json`). `DELETE ...?hard=true` deletes the row **and its photos for good**; its `#seq` is never handed out again. Restore with `PATCH {"status": "active"}` (refused with `offering_limit` if that would exceed the cap).

Archived items (`GET ?status=archived`, first item shown) carry `archived_at`:

`200` (capture `44-list-archived.json`)

```json
{
  "data": [
    {
      "id": "e7350b65-6520-492b-b373-122f7d55003d",
      "seq": 4,
      "ref": "#4",
      "kind": "product",
      "name": "Old stock",
      "description": null,
      "category": null,
      "keywords": null,
      "price_mode": "fixed",
      "price": 12000,
      "price_note": null,
      "availability": "in_stock",
      "quantity": null,
      "negotiable": false,
      "status": "archived",
      "external_ref": null,
      "images": [],
      "photo_count": 0,
      "cover_url": null,
      "availability_changed_at": "2026-10-30T23:07:04.636694+00:00",
      "archived_at": "2026-10-30T23:07:04.651032+00:00",
      "created_at": "2026-10-30T23:07:04.636694+00:00",
      "updated_at": "2026-10-30T23:07:04.651032+00:00"
    }
  ],
  "meta": {
    "next_cursor": null,
    "total": 1,
    "page_size": 50,
    "limit": 25,
    "used": 3
  }
}
```
Restore:

`PATCH /api/v1/offerings/e7350b65-6520-492b-b373-122f7d55003d`

```json
{
  "status": "active"
}
```

Response `200` (capture `45-unarchive-with-patch.json`)

```json
{
  "data": {
    "id": "e7350b65-6520-492b-b373-122f7d55003d",
    "seq": 4,
    "ref": "#4",
    "kind": "product",
    "name": "Old stock",
    "description": null,
    "category": null,
    "keywords": null,
    "price_mode": "fixed",
    "price": 12000,
    "price_note": null,
    "availability": "in_stock",
    "quantity": null,
    "negotiable": false,
    "status": "active",
    "external_ref": null,
    "images": [],
    "photo_count": 0,
    "cover_url": null,
    "availability_changed_at": "2026-10-30T23:07:04.636694+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:04.636694+00:00",
    "updated_at": "2026-10-30T23:07:04.677023+00:00"
  }
}
```
Hard delete, `204` (capture `46-hard-delete.json`); deleting again:

`404` (capture `47-hard-delete-again-not-found.json`)

```json
{
  "code": "offering_not_found",
  "message": "Offering not found"
}
```
After a hard delete, the next created item continues the numbering (`seq` 5 here, although #4 was deleted):

`201` (capture `48-create-after-hard-delete-seq-moves-on.json`)

```json
{
  "data": {
    "seq": 5,
    "ref": "#5",
    "_": "rest of the item trimmed by the guide"
  }
}
```

### 4.6 The cap

Creating, restoring from archive, and importing are refused when they would push the number of **active** items past the plan cap. Existing items always stay editable, even if a plan change left the business over its cap.

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "One too many",
  "price_mode": "fixed",
  "price": 12000,
  "availability": "in_stock"
}
```

Response `409` (capture `58-create-over-the-cap.json`)

```json
{
  "code": "offering_limit",
  "message": "Your plan allows 25 items. Archive or delete one, or upgrade, to add more.",
  "limit": 25,
  "used": 25
}
```

## 5. Photos

Rules the dashboard should enforce *before* uploading (the server enforces them all again): JPEG, PNG or WEBP only, at most 5 MB, at most the plan's photos per item. **GIF is refused. HEIC is not supported**: the file input should ask for JPEG so iOS converts. Resize to a 1,600 px long edge at quality about 80 in the browser; the server also optimises (it requests a 1,600 px long edge, automatic quality, JPEG and removal of location data from Cloudinary; the effect on real photos has not been checked yet). Every upload passes a moderation check that **fails closed**: if the check is down, the upload is refused with the same message as a flagged photo.

### 5.1 Upload

`POST /api/v1/offerings/{id}/images` as `multipart/form-data` with one file part named `file`. The call is synchronous (it waits for moderation and storage, typically seconds): show a per-photo spinner, do not poll.

`POST /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87/images`

multipart/form-data: {"file": {"filename": "front.jpg", "content_type": "image/jpeg", "size_bytes": 948}}

Response `201` (capture `14-photo-upload-first.json`)

```json
{
  "data": {
    "id": "ff9bae44-547d-42db-ac4d-ccd521915ea9",
    "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
    "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
    "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
    "position": 0,
    "width": 1200,
    "height": 900,
    "bytes": 240700
  }
}
```
`url` is the stored optimised copy, `thumb_url` a 400 px rendition (cards and lists), `gallery_url` a 1,000 px rendition (lightbox). All three are plain image URLs on the CDN. `position` 0 is the cover. The photo then appears on the item:

`GET /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87`

Response `200` (capture `16-get-with-photos.json`)

```json
{
  "data": {
    "id": "215a6240-fc7a-4286-a0d4-11a0a27fbc87",
    "seq": 1,
    "ref": "#1",
    "kind": "product",
    "name": "Canvas sneaker",
    "description": "Black, sizes 40-45",
    "category": "Shoes",
    "keywords": "trainers, sneakers",
    "price_mode": "fixed",
    "price": 12000,
    "price_note": null,
    "availability": "in_stock",
    "quantity": null,
    "negotiable": true,
    "status": "active",
    "external_ref": null,
    "images": [
      {
        "id": "ff9bae44-547d-42db-ac4d-ccd521915ea9",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
        "position": 0,
        "width": 1200,
        "height": 900,
        "bytes": 240700
      },
      {
        "id": "84cffc22-372a-4377-a5b0-2cb8eafcbe1c",
        "url": "https://res.cloudinary.com/demo/image/upload/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "thumb_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "gallery_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_1000,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/b0cc00d0fd4a4317912674ea9edb5762.jpg",
        "position": 1,
        "width": 1200,
        "height": 900,
        "bytes": 240121
      }
    ],
    "photo_count": 2,
    "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/a37d6ec3772743e99c8de52bea334fb9.jpg",
    "availability_changed_at": "2026-10-30T23:07:03.897971+00:00",
    "archived_at": null,
    "created_at": "2026-10-30T23:07:03.897971+00:00",
    "updated_at": "2026-10-30T23:07:03.897971+00:00"
  }
}
```
### 5.2 Order and delete

`PUT /api/v1/offerings/{id}/images/order` with every photo id of the item in the new order (first = cover). It answers with the whole item.

`PUT /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87/images/order`

```json
{
  "image_ids": [
    "9103bdf1-d58f-41fb-9e30-6ef8ffc2313a",
    "ff9bae44-547d-42db-ac4d-ccd521915ea9",
    "84cffc22-372a-4377-a5b0-2cb8eafcbe1c"
  ]
}
```

Response `200` (capture `18-photo-reorder.json`)

```json
{
  "data": {
    "images": [
      {
        "id": "9103bdf1-d58f-41fb-9e30-6ef8ffc2313a",
        "position": 0
      },
      {
        "id": "ff9bae44-547d-42db-ac4d-ccd521915ea9",
        "position": 1
      },
      {
        "id": "84cffc22-372a-4377-a5b0-2cb8eafcbe1c",
        "position": 2
      }
    ],
    "photo_count": 3,
    "cover_url": "https://res.cloudinary.com/demo/image/upload/c_limit,w_400,q_auto,f_jpg/v1730000000/luran/offerings/8bbbb8a9-3d4c-4026-bd3c-a3c26a2c0236/5d606477d28c42099fe38c7b4afb7058.jpg",
    "_": "other item fields trimmed by the guide"
  }
}
```
The list must name each photo exactly once (missing, extra, duplicate and another item's photo ids are all refused):

`PUT /api/v1/offerings/215a6240-fc7a-4286-a0d4-11a0a27fbc87/images/order`

```json
{
  "image_ids": [
    "ff9bae44-547d-42db-ac4d-ccd521915ea9"
  ]
}
```

Response `422` (capture `19-photo-reorder-mismatch.json`)

```json
{
  "code": "image_order_mismatch",
  "message": "The list must contain each of this item's photos exactly once."
}
```

Delete one photo: `DELETE /api/v1/offerings/{id}/images/{image_id}` answers `204` (capture `20-photo-delete.json`). If it was the cover the next photo becomes the cover and positions renumber from 0. The stored file is destroyed too. Deleting it again:

`404` (capture `21-photo-delete-not-found.json`)

```json
{
  "code": "offering_image_not_found",
  "message": "Photo not found"
}
```

### 5.3 Upload errors

Each is a flat module error.

GIF:

`422` (capture `22-photo-refused-gif.json`)

```json
{
  "code": "image_invalid",
  "message": "Image format 'GIF' not allowed. Allowed: JPEG, PNG, WEBP"
}
```

A file that is not an image:

`422` (capture `23-photo-refused-not-an-image.json`)

```json
{
  "code": "image_invalid",
  "message": "The file is not a valid JPEG, PNG or WEBP image."
}
```

HEIC (the message is meant to be shown as is):

`422` (capture `24-photo-refused-heic.json`)

```json
{
  "code": "image_invalid",
  "message": "HEIC photos are not supported. Use a JPEG, PNG or WEBP photo (on iPhone, choose 'Most Compatible' or pick the photo from the gallery)."
}
```

Over 5 MB (status 413, not 422):

`413` (capture `25-photo-too-large.json`)

```json
{
  "code": "image_too_large",
  "message": "The photo is larger than 5 MB. Choose a smaller one."
}
```

Moderation: flagged, or the check could not run. The reason is never revealed; show this text as is:

`422` (capture `26-photo-blocked-by-moderation.json`)

```json
{
  "code": "image_blocked",
  "message": "This image can't be accepted. Please try a different one."
}
```

Photo cap reached (Trial: 4 per item). Refused before any moderation or upload work:

`409` (capture `29-photo-upload-over-the-cap.json`)

```json
{
  "code": "offering_image_limit",
  "message": "Your plan allows 4 photos per item. Remove one, or upgrade, to add more.",
  "limit": 4,
  "used": 4
}
```

`503 image_storage_unavailable` (`"Photo storage is unavailable right now. Try again in a moment."`) means Cloudinary is down or not configured: the item itself is fine, offer a retry. **Not captured live** (it needs a failing storage; verified by an automated test only, see section 13).

## 6. Lapsed accounts (read-only)

When the trial or subscription has ended the catalog is **read-only**: lists and reads keep working, every write answers `403` with the entitlements body (not `402`).

`POST /api/v1/offerings`

```json
{
  "kind": "product",
  "name": "Nope",
  "price_mode": "fixed",
  "price": 12000,
  "availability": "in_stock"
}
```

Response `403` (capture `74-lapsed-create-refused.json`)

```json
{
  "code": "feature_not_in_plan",
  "feature": "offerings",
  "min_tier": "trial",
  "message": "Your plan does not include the offerings catalog. Upgrade to Trial to use it."
}
```

**UX instruction:** do not show this `message` verbatim to a lapsed account. It is the generic plan-gate wording ("Upgrade to Trial to use it"), which reads wrongly for someone whose subscription ended. Detect `code == "feature_not_in_plan"` with `feature == "offerings"` on a write and show a read-only banner with a renew action instead. Reads for the same account:

`200` (capture `73-lapsed-list-still-allowed.json`)

```json
{
  "data": [
    {
      "id": "8388d412-dd94-4fa9-aa8f-81441d98bc3d",
      "seq": 1,
      "ref": "#1",
      "kind": "product",
      "name": "Canvas sneaker",
      "description": null,
      "category": null,
      "keywords": null,
      "price_mode": "fixed",
      "price": 12000,
      "price_note": null,
      "availability": "in_stock",
      "quantity": null,
      "negotiable": false,
      "status": "active",
      "external_ref": null,
      "images": [],
      "photo_count": 0,
      "cover_url": null,
      "availability_changed_at": "2026-10-30T23:07:08.922144+00:00",
      "archived_at": null,
      "created_at": "2026-10-30T23:07:08.922144+00:00",
      "updated_at": "2026-10-30T23:07:08.922144+00:00"
    }
  ],
  "meta": {
    "next_cursor": null,
    "total": 1,
    "page_size": 50,
    "limit": 100,
    "used": 1
  }
}
```

## 7. Bulk import

The import wizard is three steps: paste or upload, preview, apply. **Both calls take the same body**; preview never saves.

Body: `format` (`csv` or `lines`), `content` (the text), `mode` (`add` default, or `upsert`), `skip_errors` (apply only, default `false`). Limits: **500 rows and 256 KB per call**.

* **CSV**: a header row is required with a `name` column. Recognised columns: `name, kind, price, price_mode, price_note, availability, category, description, keywords, quantity, negotiable, external_ref` (a few aliases such as `item`, `amount`, `stock`, `sku`; other columns are ignored). Comma, tab or semicolon separated (pasting from a spreadsheet works). Amounts accept `12,000`, `₦12000`, `12k`, `1.5m`, `from 5k`. Availability accepts `in stock`, `sold out`, `limited`, `on request` and a few more.
* **`lines`**: one item per line, `Name - 12000 - in stock` (the list a seller pastes from WhatsApp). Only name, price and availability.
* Defaults: `kind` product, `availability` in_stock, `price_mode` fixed when a price is given and on_request when it is blank.
* A name may not start with `= + - @`.
* **Row numbers** (`row`) count data rows only: the header and blank lines are not counted.

### 7.1 Preview

`POST /api/v1/offerings/import/preview`

```json
{
  "format": "csv",
  "content": "name,kind,price,availability,category,external_ref\nTote bag,product,8500,in stock,Bags,SKU-TOTE\nBraiding,service,from 5k,limited,Services,SKU-BRAID\nBad price,product,abc,in stock,Bags,SKU-BAD\n",
  "mode": "add"
}
```

Response `200` (capture `49-import-preview-csv.json`)

```json
{
  "data": {
    "rows": [
      {
        "row": 1,
        "status": "create",
        "parsed": {
          "kind": "product",
          "name": "Tote bag",
          "price_mode": "fixed",
          "price": 8500,
          "availability": "in_stock",
          "category": "Bags",
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": "SKU-TOTE"
        },
        "errors": []
      },
      {
        "row": 2,
        "status": "create",
        "parsed": {
          "kind": "service",
          "name": "Braiding",
          "price_mode": "from",
          "price": 5000,
          "availability": "limited",
          "category": "Services",
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": "SKU-BRAID"
        },
        "errors": []
      },
      {
        "row": 3,
        "status": "error",
        "parsed": null,
        "errors": [
          {
            "field": "price",
            "code": "price_invalid"
          }
        ]
      }
    ],
    "summary": {
      "create": 2,
      "update": 0,
      "error": 1,
      "over_limit_by": 0
    }
  }
}
```
`parsed` is exactly what would be saved. `status` is `create`, `update` or `error`; error rows have `parsed: null`. `summary.over_limit_by` is how many of the new items would not fit under the plan cap (0 when everything fits).

The pasted-list format:

`POST /api/v1/offerings/import/preview`

```json
{
  "format": "lines",
  "content": "Leather belt - 4500 - in stock\nTailoring - from 2k\n",
  "mode": "add"
}
```

Response `200` (capture `50-import-preview-lines.json`)

```json
{
  "data": {
    "rows": [
      {
        "row": 1,
        "status": "create",
        "parsed": {
          "kind": "product",
          "name": "Leather belt",
          "price_mode": "fixed",
          "price": 4500,
          "availability": "in_stock",
          "category": null,
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": null
        },
        "errors": []
      },
      {
        "row": 2,
        "status": "create",
        "parsed": {
          "kind": "product",
          "name": "Tailoring",
          "price_mode": "from",
          "price": 2000,
          "availability": "in_stock",
          "category": null,
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": null
        },
        "errors": []
      }
    ],
    "summary": {
      "create": 2,
      "update": 0,
      "error": 0,
      "over_limit_by": 0
    }
  }
}
```
### 7.2 Apply

**Atomic by default:** one bad row refuses the whole import and nothing is saved.

`POST /api/v1/offerings/import`

```json
{
  "format": "csv",
  "content": "name,kind,price,availability,category,external_ref\nTote bag,product,8500,in stock,Bags,SKU-TOTE\nBraiding,service,from 5k,limited,Services,SKU-BRAID\nBad price,product,abc,in stock,Bags,SKU-BAD\n",
  "mode": "add"
}
```

Response `422` (capture `51-import-apply-atomic-refused.json`)

```json
{
  "code": "import_has_errors",
  "message": "Some rows are not valid, so nothing was imported. Fix them or skip them.",
  "errors": [
    {
      "row": 3,
      "errors": [
        {
          "field": "price",
          "code": "price_invalid"
        }
      ]
    }
  ],
  "summary": {
    "create": 2,
    "update": 0,
    "error": 1,
    "over_limit_by": 0
  }
}
```
With `skip_errors: true` the valid rows are created and the bad ones reported:

`POST /api/v1/offerings/import`

```json
{
  "format": "csv",
  "content": "name,kind,price,availability,category,external_ref\nTote bag,product,8500,in stock,Bags,SKU-TOTE\nBraiding,service,from 5k,limited,Services,SKU-BRAID\nBad price,product,abc,in stock,Bags,SKU-BAD\n",
  "mode": "add",
  "skip_errors": true
}
```

Response `200` (capture `52-import-apply-skip-errors.json`)

```json
{
  "data": {
    "created": 2,
    "updated": 0,
    "skipped": 1,
    "errors": [
      {
        "row": 3,
        "errors": [
          {
            "field": "price",
            "code": "price_invalid"
          }
        ]
      }
    ]
  }
}
```
### 7.3 Upsert

`mode: "upsert"` updates existing items instead of duplicating them. Rules: a row with `external_ref` matches the item with that ref (any status; an archived match is updated and **reactivated**, which counts toward the cap). A row without a ref matches an **active** item with the same name, case-insensitively; two such items make the row an `ambiguous_name` error. **Blank cells and absent columns leave the stored value unchanged** (pasting a partial list never wipes descriptions). Another business's items are never matched. In `add` mode a row whose `external_ref` already exists is an error (`external_ref_taken`).

`POST /api/v1/offerings/import/preview`

```json
{
  "format": "csv",
  "content": "name,price,external_ref\nTote bag (new price),9000,SKU-TOTE\nFresh item,100,SKU-NEW\n",
  "mode": "upsert"
}
```

Response `200` (capture `53-import-preview-upsert.json`)

```json
{
  "data": {
    "rows": [
      {
        "row": 1,
        "status": "update",
        "parsed": {
          "kind": "product",
          "name": "Tote bag (new price)",
          "price_mode": "fixed",
          "price": 9000,
          "availability": "in_stock",
          "category": "Bags",
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": "SKU-TOTE"
        },
        "errors": [],
        "target": "#6"
      },
      {
        "row": 2,
        "status": "create",
        "parsed": {
          "kind": "product",
          "name": "Fresh item",
          "price_mode": "fixed",
          "price": 100,
          "availability": "in_stock",
          "category": null,
          "description": null,
          "keywords": null,
          "price_note": null,
          "quantity": null,
          "negotiable": false,
          "external_ref": "SKU-NEW"
        },
        "errors": []
      }
    ],
    "summary": {
      "create": 1,
      "update": 1,
      "error": 0,
      "over_limit_by": 0
    }
  }
}
```
`target` is the `ref` of the item an update row will change. Applying it:

`200` (capture `54-import-apply-upsert.json`)

```json
{
  "data": {
    "created": 1,
    "updated": 1,
    "skipped": 0,
    "errors": []
  }
}
```
### 7.4 Import errors

`422` (capture `55-import-unreadable.json`)

```json
{
  "code": "import_unreadable",
  "message": "The import could not be read: the header needs a 'name' column."
}
```

`422` (capture `56-import-too-many-rows.json`)

```json
{
  "code": "import_too_large",
  "message": "The import has 501 rows; the limit is 500. Split it into parts."
}
```
Over the plan cap, apply refuses the **whole** import and says how many fit (`can_add`); preview warns through `summary.over_limit_by` instead:

`409` (capture `59-import-over-the-cap.json`)

```json
{
  "code": "offering_limit",
  "message": "Your plan allows 25 items and you have 25. This import would add 3; only 0 fit.",
  "limit": 25,
  "used": 25,
  "can_add": 0
}
```

`200` (capture `60-import-preview-over-the-cap.json`)

```json
{
  "data": {
    "rows": "(3 rows trimmed by the guide)",
    "summary": {
      "create": 3,
      "update": 0,
      "error": 0,
      "over_limit_by": 3
    }
  }
}
```
`import_too_large` is also returned for content over 256 KB (same code, message about the size; only the row-count variant was captured live).

## 8. Confirm, settings, Not-found

### 8.1 "Everything is still correct today"

One tap (`POST /api/v1/offerings/confirm`, no body) stamps the time. It is the daily habit. The design (not yet built or verified here) is that the AI adds an "as of <date>, please confirm" caveat when the list is older than `stale_after_days` and that this tap resets it; that behaviour belongs to part 2.

`POST /api/v1/offerings/confirm`

Response `200` (capture `61-confirm-today.json`)

```json
{
  "data": {
    "confirmed_at": "2026-10-30T23:07:04.967450+00:00"
  }
}
```
### 8.2 Settings

`GET /api/v1/offerings/settings` never writes: a business that has never touched them gets the defaults (currency from the sales agent setting, else NGN).

`200` (capture `05-settings-default.json`)

```json
{
  "data": {
    "enabled": true,
    "currency": "NGN",
    "stale_after_days": 7,
    "confirmed_at": null
  }
}
```

After a confirm:

`200` (capture `62-settings-after-confirm.json`)

```json
{
  "data": {
    "enabled": true,
    "currency": "NGN",
    "stale_after_days": 7,
    "confirmed_at": "2026-10-30T23:07:04.967450+00:00"
  }
}
```
`PATCH` takes any subset of `enabled`, `stale_after_days` (0 to 365; 0 = never add the caveat), `currency` (three letters, upper-cased by the server):

`PATCH /api/v1/offerings/settings`

```json
{
  "stale_after_days": 3,
  "currency": "ngn"
}
```

Response `200` (capture `63-settings-patch.json`)

```json
{
  "data": {
    "enabled": true,
    "currency": "NGN",
    "stale_after_days": 3,
    "confirmed_at": "2026-10-30T23:07:04.967450+00:00"
  }
}
```
Validation uses shape 2:

`PATCH /api/v1/offerings/settings`

```json
{
  "stale_after_days": 400,
  "currency": "NAIRA"
}
```

Response `422` (capture `64-settings-validation.json`)

```json
{
  "code": "validation",
  "message": "Some fields are not valid.",
  "errors": [
    {
      "field": "stale_after_days",
      "code": "stale_after_days_invalid"
    },
    {
      "field": "currency",
      "code": "currency_invalid"
    }
  ]
}
```

`enabled: false` is the switch part 2 will read to stop the AI using the catalog for that business without deleting anything. In this part it is only stored and returned.

### 8.3 Not-found: what customers asked for that is not listed

**Capture note:** the rows in these captures were inserted with SQL because the AI path that records them is a later stream. The read and dismiss endpoints are real. Terms are normalised and aggregated: there is **no customer, no conversation and no raw message** in this data. Terms unseen for 90 days are deleted by a daily job (the job itself was run by the real worker, capture `70-worker-sweep-old-misses.json`).

`GET /api/v1/offerings/misses`

Response `200` (capture `65-misses-list.json`)

```json
{
  "data": [
    {
      "id": "86b1066f-d272-4c5c-950b-c04faba0f6aa",
      "term": "gym bag",
      "count": 9,
      "last_seen_at": "2026-10-30T21:07:05.006434+00:00"
    },
    {
      "id": "c8fe6f94-94ab-4aaa-9fa4-6e4469c48b6b",
      "term": "red heels",
      "count": 5,
      "last_seen_at": "2026-10-29T23:07:05.006434+00:00"
    }
  ]
}
```
Window: `?days=` 1 to 90, default 30; most asked first, then most recent. A wider window includes older terms:

`GET /api/v1/offerings/misses?days=90`

Dismiss a term (`204`, no body; capture `68-misses-dismiss.json`). Dismissing again:

`404` (capture `69-misses-dismiss-again-not-found.json`)

```json
{
  "code": "offering_miss_not_found",
  "message": "Not-found term not found"
}
```
The dashboard's "Add this" button opens the add sheet pre-filled with `term`; it does not call any special endpoint.

## 9. Wanted items on a lead

**Capture note:** the `interests` rows were inserted with SQL (the AI path writes them in a later stream). The read is real. `GET /api/v1/leads/{id}`, the lead status `PATCH` and the lead score `POST` all return the same detail, and **all three now carry `interests`**; it is the only new field, always present (empty list when there are none), ordered oldest first.

Each entry is a **snapshot of what the customer was shown**: later edits or deletion of the item never change it. `offered_price` is the figure the customer proposed for a negotiable item (whole naira), else `null`.

The new field only (the rest of the lead detail is unchanged, see its own guide; capture `71-lead-detail-with-interests.json`):

```json
{
  "interests": [
    {
      "ref": "#1",
      "name": "Canvas sneaker",
      "price_mode": "fixed",
      "price": 14000,
      "price_note": null,
      "qty": 2,
      "offered_price": 12000
    },
    {
      "ref": "#3",
      "name": "Tote bag",
      "price_mode": "fixed",
      "price": 9000,
      "price_note": null,
      "qty": 1,
      "offered_price": null
    }
  ]
}
```

## 10. Downgrades are blocked by the catalog

A downgrade is refused while active items exceed the target plan's cap, or any one item holds more photos than the target plan allows. Archived items do not count. Nothing is trimmed silently. This is the existing downgrade endpoint; two new violation fields appear:

`POST /api/v1/subscription/downgrade`

```json
{
  "tier": "lite",
  "reason": "capture"
}
```

Response `400` (capture `76-downgrade-blocked-by-catalog.json`)

```json
{
  "detail": {
    "error": "data_exceeds_limits",
    "violations": [
      {
        "field": "offerings",
        "current_value": 120,
        "allowed_value": 100,
        "difference": 20,
        "action_required": "Archive or delete 20 items in your offerings before downgrading"
      },
      {
        "field": "offering_images",
        "current_value": 5,
        "allowed_value": 4,
        "difference": 1,
        "action_required": "Remove 1 photo from the item with the most photos before downgrading"
      }
    ],
    "message": "Your current usage exceeds the new tier's limits"
  }
}
```

**UX instruction:** show each `action_required` as written and link to the Offerings page. The `field` values are `offerings` and `offering_images`.

## 11. UX consequences the dashboard must surface

| What the backend does | What the dashboard must do |
|---|---|
| Archived items do not count toward the cap and are invisible to the AI | Make "Archive" the default remove action; say "removed from your AI's list" |
| `DELETE ?hard=true` destroys the item **and its photos** for good | Ask for explicit confirmation, name the photo count |
| A `#seq` is never reused after deletion | Do not promise renumbering; show `ref` as the stable handle |
| Switching price mode to "on request" clears the price | Warn before saving; the old price is gone |
| Blank cells in an upsert import keep the stored value | Say so in the wizard ("blank cells are left as they are") |
| One bad row refuses the whole import | Offer "Skip errors and import the rest" as a separate choice |
| An import over the cap is refused whole | Use `summary.over_limit_by` in the preview to disable "Import" and show the upgrade nudge before the call |
| A lapsed account is read-only | Detect `feature_not_in_plan` on writes, show a renew banner, do not show the raw message |
| Moderation failure and a flagged photo look identical | Show the fixed message as is; never suggest the photo was "flagged" |
| HEIC is unsupported | Ask the file input for JPEG; show the HEIC message as is |
| Photos are optimised on the server | Do not show the stored size; the displayed sizes are the `thumb_url` / `gallery_url` renditions |
| `confirm` stamps `confirmed_at` | After the tap, show "Confirmed <time>" from `confirmed_at` |
| Not-found terms hold no customer data | Do not imply you can contact the person who asked |
| A downgrade is blocked while the catalog exceeds the target plan | Show each `action_required` and link to the Offerings page |

## 12. Error code reference

`errors[]` entries (shape 2) carry only `field` and `code`; write your own copy. "Live" means the code appears in a capture; the others are exercised by automated tests only.

| Code | Where | Seen live |
|---|---|---|
| `price_required`, `price_not_allowed`, `price_out_of_range` | create, patch | live |
| `name_required` | create, patch | live |
| `kind_required`, `price_mode_required`, `availability_required` (a `null` sent in a patch) | patch | tests only |
| `name_formula` (a name that starts with `=`, `+`, `-` or `@` once invisible characters are removed) | create, patch, import rows | tests only (import preview `name_formula`: see the row below) |
| `stale_after_days_invalid`, `currency_invalid` | settings | live |
| `name_too_long` (over 120), `description_too_long` (over 2,000), `category_too_long` (60), `keywords_too_long` (200), `price_note_too_long` (60), `quantity_invalid` (below 0 or above 2,147,483,647; also a CSV `quantity` cell) | create, patch, import rows | tests only |
| `price_invalid`, `name_formula`, `duplicate_in_file`, `external_ref_taken`, `ambiguous_name`, `kind_invalid`, `availability_invalid`, `price_mode_invalid`, `negotiable_invalid` | import rows | `price_invalid` live; the rest tests only |

Top-level module error codes (shape 1):

| `code` | Status | Extra keys | Seen live |
|---|---|---|---|
| `offering_not_found` | 404 | | live |
| `offering_limit` | 409 | `limit`, `used` (import adds `can_add`) | live |
| `offering_image_limit` | 409 | `limit`, `used` | live |
| `offering_image_not_found` | 404 | | live |
| `offering_miss_not_found` | 404 | | live |
| `validation` | 422 | `errors[]` | live |
| `invalid_cursor` | 422 | | live |
| `image_invalid`, `image_too_large` (413), `image_blocked`, `image_order_mismatch` | 422 / 413 | | live |
| `import_unreadable`, `import_too_large`, `import_has_errors` | 422 | `import_has_errors` has `errors[]` and `summary` | live (`import_too_large` row variant only) |
| `line_too_long` (a pasted `lines` line over 1,000 characters; the message names the line) | 422 | | tests only |
| `import_conflict` (a database refusal while applying; nothing was imported; preview again) | 409 | | tests only |
| `offering_archive_full` (active + archived rows reached the per-plan ceiling: 4 x the plan's item cap, at least 200; 20,000 on an unlimited plan) | 409 | `limit`, `used` (import adds `can_add`) | tests only |
| `offering_archived` (a photo upload to an archived item; restore the item first) | 409 | | tests only |
| `image_storage_unavailable` | 503 | | **not live** (test only) |
| `feature_not_in_plan` | 403 | `feature`, `min_tier` | live |

## 13. Verification table

| Behaviour | Verified |
|---|---|
| Create, get, list, filters, search, cursor paging, patch, clear-with-null | **live** (captures 06 to 47) |
| Price rules and all-errors-together validation shape | **live** |
| Framework 422 shape for wrong types and out-of-range query | **live** |
| `#seq` continues after a hard delete; archive / restore / hard delete | **live** |
| Plan cap on create and import; `can_add`; `over_limit_by`; meter on the entitlements endpoint | **live** |
| Photo upload, order, delete, reorder mismatch, per-item photo cap (Trial 4) | **live, against a stand-in Cloudinary and stand-in moderation** |
| GIF, text file, HEIC, over 5 MB, moderation block messages | **live (stand-in moderation)** |
| Real Cloudinary optimisation result (`bytes`, 1,600 px, EXIF stripped, the 800 KB retry), real moderation | **not exercised** (unit tests with a mocked uploader only) |
| `503 image_storage_unavailable` | **not exercised live** (tests only) |
| Upload rate limit (`429`) | **not exercised** (limits are 200 uploads/hour per IP and 300/hour per tenant; the per-tenant key is asserted by a test, the 429 itself never fired) |
| Review fixes: `null` on required fields, quantity range, formula guard on create/patch, `line_too_long`, `import_conflict`, `offering_archive_full`, `offering_archived` | tests only (bodies asserted by `tests/offerings/test_review_fixes.py`; **not re-captured live**) |
| CSV and lines preview, atomic apply, skip errors, upsert, unreadable, too many rows | **live** |
| Import over 256 KB, formula-leading names, tab/semicolon CSV, duplicate refs | tests only |
| Confirm, settings (get/patch/validation) | **live** |
| Not-found list, window, dismiss | **live** (rows inserted by SQL; the writer is a later stream) |
| 90-day Not-found sweep runs in the real worker | **live** (enqueued on the arq queue, run by the worker process) |
| Lead detail `interests` | **live** (rows inserted by SQL; the writer is a later stream) |
| Lapsed account: reads allowed, writes `403 feature_not_in_plan` | **live** |
| Downgrade blocked by item count and by photos per item | **live** |
| Concurrent creates cannot overshoot the cap or share a `#seq` | tests only (real Postgres, 10 threads) |
| Orphan photo sweep (daily) and tenant-purge destroying photos | tests only |
| Reads by a support agent allowed, writes refused | **live** |

## 14. Follow-ups for this part

* The per-item photo limit is not exposed as a number before an upload attempt; a field next to `limit`/`used` on the entitlements entry would let the dashboard show "2 of 4" up front (needs a decision: it is a change to the shared entitlements response).
* The lapsed-account `403` carries the generic plan-gate text ("Upgrade to Trial to use it"); the dashboard must not show it (section 6). A tailored message is a backend follow-up.
* Photo `bytes`/dimension behaviour on real photos is unverified until the first run against the real Cloudinary account. EXIF/GPS removal is **not verified** either: the upload asks Cloudinary to strip metadata (`strip_profile`) but nobody has looked at a stored photo; do not promise customers or tenants that location data is removed until the staging check (a GPS-tagged JPEG) has been done.
* A framework `422` with a `detail` array (not the flat shape) is also returned for an import `content` over 1,048,576 characters. Normal oversize imports (over 256 KB) still get `import_too_large`.
