# Theme gallery integration

The gallery loads `/api/designs/`, whose entries now include `preview_available`, `demo_url` and `demo_store_slug`. Ready themes remain visible; Preview is shown only when the backend has an enabled, validated demo. The dedicated preview route re-resolves `/api/theme-previews/<key>/`, so a disabled or unpublished theme also fails closed on direct navigation.

Each card distinguishes Standard and Premium themes and links to the existing selection flow. Authenticated visitors continue to `/create?design=<key>`. Anonymous visitors continue to `/signup?design=<key>`; signup stores the validated key in local/session storage and login returns to store creation. The existing creation and membership workflows remain authoritative, so this does not bypass premium entitlements.

Gallery images generated from live demos are written under `apps/showroom/public/gallery/generated/`. Promote reviewed captures according to the existing gallery asset workflow; never substitute generic imagery for a different theme.
