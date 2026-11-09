# Preset architecture

`theme_previewer.presets.THEME_PRESETS` is the registry and API source. A preset belongs to one theme and contains only supported visual values: key, label, description, accent and surface.

The showroom appends `preview_preset` to the selected official demo URL. The public storefront config endpoint applies it only when the resolved tenant has a `ThemeDemoStore` profile. Unknown preset keys fail closed to the tenant's saved configuration. No merchant row is changed.

The selected preset is carried as `preset` alongside `design` into signup or store creation. Existing design entitlement checks remain authoritative. The preset is not presented as a merchant mutation and never bypasses premium access.
