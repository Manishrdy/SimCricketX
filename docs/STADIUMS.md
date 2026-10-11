# Stadiums and venue compatibility

Current stadium tuning uses official and private v3 revisions. The v2 documentation below describes preserved setups and snapshots.

## Simple profiles

Stadiums have five editable inputs: name, one default pitch per format, outfield speed, four fixed boundary distances, and average temperature. Numerical starter values are **simulation presets**, not measured historical datasets or calibrated statistical fits.

`stadiums` retains stable UUIDs, slugs, owners, official/custom kind, active/retired status, current revision and audit timestamps. `stadium_revisions_v2` stores immutable revisions with composite key `(stadium_id, revision)`:

| Field | Type / rule | Default |
| --- | --- | --- |
| `schema_version` | Integer, server managed | 2 |
| `name` | Trimmed string, 1–100 characters | Required |
| `pitch_defaults` | JSON: valid pitch key for T10, T20, Hundred, ListA and FC | Hard |
| `outfield_speed` | Finite float, 0.90–1.10 | 1.00 |
| `dimensions_m` | JSON: `straight_a`, `straight_b`, `square_a`, `square_b`, each a finite 40–100 m distance | 70 m each |
| `average_temperature_c` | Finite float, −20–55°C | 22°C |
| `created_at` | UTC revision timestamp | Server managed |

Country, timezone, location metadata, descriptions, advanced JSON editing, boundary ranges, preparation probabilities, monthly climates, drainage and stadium dew profiles are no longer part of current profiles. Unknown profile fields are rejected. The global/per-user format configurations remain the engine baseline.

## Conditions model

New matches use `stadium_v2`. Match setup preselects the chosen stadium's pitch for the format and permits a match-specific pitch override. Changing stadium or format resets that choice to the corresponding default. Fixture stadium assignment takes precedence over explicit match selection, then the home stadium.

At creation the server freezes stadium identity/revision/name, pitch, dimensions, speed, temperature, coefficients, temperature wear rate, format/day-night flag, original delivery budget, effective engine configuration, weather script and conditions seed. Play and resume use this snapshot without catalogue reads. Stadium changes and retirement never rewrite saved matches. Browser projections omit RNG seeds, effective configuration, scripts and checkpoints.

For `r`, the mean of the four boundary distances, outcome weights receive:

- Six: `clamp((70 / r)^2, 0.85, 1.15)`.
- Four: `clamp((70 / r) * outfield_speed, 0.90, 1.10)`.
- Double: `clamp(r / 70, 0.95, 1.10)`.
- Three: `clamp((r / 70)^2, 0.90, 1.15)`.

These adjustments precede momentum/pressure and existing probability normalization. Fixed dimensions describe an average ground size; the simulation does not model directional ball flight.

Temperature derives `clamp(1 + 0.01 * (average_temperature_c - 22), 0.85, 1.15)` as a pitch-wear multiplier. Apply it to existing stadium wear progress, capped at full wear. Limited overs accumulate deliveries across innings against the original full-match budget with a 240-ball minimum reference. Rain reductions do not accelerate wear. FC retains day-aware progression. Tie breakers carry the main match's wear. Existing pitch-specific wear effects translate that deterioration into play; temperature adds no independent scoring, wicket, fatigue or dew modifier. Captaincy/toss may consult the public derived wear multiplier.

Weather and day/night remain ordinary match settings. Standard weather scripts are generated once with a saved seed, with normal rain recovery and no stadium drainage multiplier. List A and Hundred use their existing format-specific dew layers; FC retains its existing day/night behavior. V2 adds no stadium dew layer.

## Interfaces and ownership

All stadium routes require login; writes use the application's CSRF protection.

- `GET /stadiums` and `GET /api/stadiums?q=&kind=&page=` search visible active stadiums by name and official/private collection. API pages contain 25 records.
- `GET /stadiums/new`, `GET /stadiums/<id>` display the simple editor/detail page; POST saves a new immutable revision.
- `GET /api/stadiums/<id>` returns identity, schema version and current simple profile, including accessible retired records.
- `POST /api/stadiums` creates a private stadium from the five profile fields.
- `PATCH /api/stadiums/<id>` accepts `{revision, profile}`, merges profile fields at the top level and rejects stale saves.
- `POST /api/stadiums/<id>/preview` accepts only `{match_format}` (default T20) and returns name/revision, selected default pitch, dimensions, speed, temperature and derived wear multiplier. No date/seasonal sampling.
- `POST /api/stadiums/<id>/retire` retires the owner's custom ground.
- `POST /api/team/<id>/stadium` adopts/changes via `{stadium_id}` without rewriting squad or legacy fields.

Ordinary users edit their own private grounds; admins may edit official grounds. Retired stadiums cannot be selected for new teams/matches. Copying an official profile creates a private profile. Home's Stadiums card and Use for new team links retain their existing behavior.

Match setup accepts `stadium_id` and `condition_overrides` containing only `pitch`. Weather uses `weather_forecast`; day/night uses `is_day_night`. The server supplies all snapshots/model versions and discards client-supplied versions and snapshots.

Existing teams retain legacy text grounds until explicitly linked. New/imported teams require a stadium to publish/play; incomplete drafts may omit it. Linked teams cannot downgrade to legacy mode. Clones retain an accessible active stadium or require replacement.

## Migration and compatibility

Startup creates v2 tables and converts existing current profiles before catalogue seeding/reads. Conversion adds revision `old_current_revision + 1`, preserving the stadium ID and links:

- Choose each format's highest-weight pitch; ties follow existing pitch-option order.
- Take the typical value of each old boundary range and the old speed multiplier.
- Average the twelve monthly typical temperatures, rounded to one decimal.

Conversion is transactional and idempotent. The original `stadium_revisions` table remains historical storage; new reads/writes use `stadium_revisions_v2`. Saved match snapshots remain untouched. Existing `stadium_v1` matches retain their original modifiers, wear, dew and weather scripts through isolated compatibility code. Unversioned/legacy matches retain the legacy engine path. Summaries render both versions.

`STADIUM_MATCHES_ENABLED=false` disables new stadium matches while existing saved matches remain resumable. Deployment rollback must preserve v2 tables and support for already-created v2 matches.

Validation uses shared QA accounts and isolated databases, including migration preservation/idempotence, access control, field rejection, format defaults, geometry/outfield direction and caps, temperature wear, ordinary weather/dew behavior, all five formats, checkpoints and saved-match compatibility.


## Private stadium tuning (v3)

Stadiums is now the only user-facing conditions editor. Official catalogue venues are editable by admins and read-only to ordinary users. **Add to My stadiums** on a catalogue card creates a private copy directly, without opening the detail page. It uses the adoption API with CSRF protection and refreshes the catalogue in place; a normal POST form returns to My stadiums without JavaScript. Explore stadium opens the detail page. Personal copies follow official updates until the user saves their own changes, which become a complete private override with independent revision history. Newly created stadiums are also private v3 profiles. New team, clone and neutral-venue selections require adopted private stadiums; existing legacy team/fixture links remain usable.

`stadiums.current_profile_version` explicitly selects the revision table (2 or 3). `stadium_revisions_v3` retains the five simple venue fields and adds `format_settings`, `baseline_configs` and `baseline_provenance`. V1/v2 rows and all saved match snapshots remain untouched. The additive `add_stadium_tuning` startup migration is idempotent and adopts nothing.

Each format block contains:

- `blending`: `pitch_weight` and `skill_weight`, finite values from 0 to 1 that sum to 1.
- `pitches`: each existing pitch name maps to `scoring`, `wicket`, `pace` and `spin` adjustments.
- `phases` (excluded for FC): `powerplay`, `middle`, `death`, each with `scoring`, `boundary`, `wicket`, and `chasing: {scoring, wicket}`.

Adjustments accept finite values from 0× to 3×. All start at 1× the captured baseline. Scoring applies to Single, Double, Three, Four and Six, while Dot and Extras remain unchanged. Overall wickets compound with the classified pace/spin wicket adjustment. The engine classifies Fast, Fast-medium and Medium-fast as pace; Off spin, Leg spin, Finger spin and Wrist spin are spin. Default/unclassified styles, including Medium, receive only the overall adjustment.

Baselines capture the user's effective tuning for all five formats at creation/adoption, including detailed style affinities, scoring matrices, game modes, wear, dew and FC ball conditions. Provenance records user/factory source, capture version and UTC capture time. Later revisions keep the same captured baseline. Database read errors abort capture rather than silently substituting factory tuning. No ordinary browser API exposes baseline configurations or provenance.

`POST /api/stadiums/<id>/adopt` accepts `{revision}`. An official template creates a private copy (re-adding it at a later source revision reuses the active personal copy); an owned v2 stadium advances to a v3 revision under the same ID. Existing teams/fixtures are never relinked automatically. Adopting an owned stadium changes future matches for every team using it; saved matches keep their conditions. A `stadium_adoptions` receipt keyed by owner/source/revision makes retries return the same stadium, even after subsequent edits. Retired copies cannot be silently recreated by retrying an old adoption request.

Existing create/edit APIs support `format_settings`; baseline/provenance/model-version fields are server-owned and rejected from client profiles. PATCH remains `{revision, profile}` with top-level profile merging and full validation. The HTML editor supports independent format/pitch panels, chasing disclosures, a complementary blending slider and local draft recovery scoped by user/stadium/revision. Revision conflicts retain submitted inputs. Successful saves clear only that draft.

Preview accepts `{match_format, scheduled_overs?}` and returns safe format settings plus zero-based half-open legal-ball phase ranges. `scheduled_overs` uses the public format catalogue's supported lengths, including List A 40/50. FC returns no phase ranges. The editor displays conventional one-based over/ball ranges.

New adopted matches freeze `stadium_v3` snapshots containing captured format configuration, resolved blending, simple adjustments, pitch, revision, venue geometry, temperature, weather scripts and seed. Match play/resume reads only the snapshot. Pitch and phase adjustments apply once after existing pitch/phase/wear/dew effects and before venue geometry, momentum and pressure. Tie breakers use frozen blending and pitch adjustments while omitting regular phase/chasing adjustments.

V3 phases use integer legal balls completed before the delivery. Full Hundred innings use powerplay 1–25, middle 26–80 and death 81–100. Reduced Hundred allocations use existing format rules, including partial-set powerplay transitions; wides/no-balls do not advance the count. T10/T20/List A use their own effective format definitions. FC has no phases. Existing model versions retain their prior paths. Captured legacy T20 phase timing remains in the internal baseline; new stadium adjustments use format-defined ranges. Hundred's baseline phase lookup also uses the exact legal-ball position in v3.

`STADIUM_V3_ENABLED=false` blocks new creation, adoption and v3 match creation, while saved v3 matches remain playable. Existing stadium editing, readers and older model support remain available. Rollback must retain a v3-capable reader if any v3 matches have been created.

The former `/ground-conditions` editor redirects to Stadiums, its guide redirects to `/stadiums/conditions-guide`, and save/mode/reset endpoints return 410 Gone after authentication. UserGroundConfig records and internal defaults/loaders remain for unadopted setups and baseline capture. Dashboard, setup, public navigation and onboarding copy now direct users to Stadiums.


### Official updates and personal overrides

Admins can save official profiles through the HTML editor or PATCH API. The first admin save promotes an official v2 venue to v3 using factory baselines, independent of the admin's personal GroundConfig. The original v2 revision remains immutable.

An official save atomically appends v3 revisions to active adopted copies that have no personal override, updating simple venue details and format settings. Team/fixture IDs and captured personal baselines stay unchanged. Adoption provenance records `official_source_id`, `official_source_revision` and `personal_override` for each format. Existing copies without this marker follow official updates only if still on their initial revision; previously edited copies remain independent. A private save that changes any setting marks the entire profile as a personal override (an unchanged save continues following official updates), so later admin updates do not replace any of its fields. New adopters receive the current official profile and format settings. Revision checks protect both official saves and propagated copy updates from stale writes. Saved matches continue to use their frozen snapshots.
