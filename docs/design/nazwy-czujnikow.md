# Design: renaming a sensor on the Garmin Connect account

Status: proposed, waiting for review; nothing is implemented. The request that renames a sensor is
not known yet (section 2): no step of section 8 starts before it is. D1 is chosen: a separate
analysis session on Garmin Connect for Android.

## 1. Scope

Give a sensor on the Garmin Connect account (a heart rate strap, a Varia, a speed sensor…) a new
name, from the window and from the command line, as Garmin Connect's phone app allows. Only the
name: nothing else of the sensor, its pairing or its battery. Only sensors the account lists
(`openexpress sensors`, the row Account Sensors of the device page; `docs/design/odczyty-z-konta.md`).

## 2. What is known, and what is not

**Known**, from the account's answers for openExpress's own sign-in: `GET /device-service/sensors`
lists the sensors; each item has `deviceName` (the name shown), `sensorType`, `batteryLevel`,
`batteryStatus`, `lastConnected`, `rechargeableSensorCapable`, and also `manufacturer`,
`partNumber`, `productId`, `serialNumber`, `softwareVersion`, `imageUrl`, `prioritySensor` and
`lastLowBatteryNotification`. No field is named as the sensor's identifier on the account; only
`serialNumber` tells two sensors of one model apart.

**Not known:** the request that changes a name (its method, address and body), which field names the
sensor in it, what it answers, the limits of a name (length, characters), and whether the new name
reaches the devices the sensor is paired with.

**How to learn it** (D1, chosen: (a)), facts only, written into a private note with where each was
learnt:
- (a) a separate analysis session on Garmin Connect for Android: the method, the address, the fields
  of the body and of the answer, the limits the app checks before sending;
- (b) the user renames one sensor on Garmin Connect's web page, if it offers that, with the browser's
  network panel open, and notes the method, the address and the body (no cookie, no token), then
  renames it back; openExpress sends nothing for it.

## 3. The window

- In the opened row Account Sensors, each sensor's row opens the dialog **Rename Sensor** when
  clicked, with an edit icon at its end (D2): a row that opens something, as elsewhere in
  libadwaita; five buttons with labels in a list would crowd the narrowest window.
- The dialog goes through `confirm()` with an entry row as its extra widget, as the course name does
  on the Courses page:
  - heading "Rename Sensor"; body: its name now; the entry holds that name, selected;
  - answers Cancel and **Rename** (suggested), Rename enabled only while the new name differs from the
    old one and keeps to the limits (section 5); when it does not, a line under the entry says why.
- Rename: a task with the row's spinner; the change sent, then the list asked anew (D4), so that the
  row shows the name the account now has. Done: a toast "Renamed to “{name}”". Not done: `inform()`
  "Sensor Not Renamed" with the reason; the row keeps its name.
- Not signed in, or the list could not be read: there is no list, so nothing to rename.

## 4. The command line

```
openexpress sensors rename NAME NEW_NAME [--apply]
```

- NAME: the sensor's name as `openexpress sensors` prints it, whole and exact (D3). No sensor or two
  of that name: refused with what was found (exit code 1), nothing sent.
- Without `--apply` (as every command that writes): "Would rename “Speed” to “Speed, rear wheel” on
  the Garmin account. Add --apply to do it." – the list asked once (or kept), nothing changed.
- With `--apply`: the change, then the list read back (D4); "Renamed “Speed” to “Speed, rear wheel”."
- Exit codes as now: 0 done, 1 an error of the account or of the name, 2 a mistake in the command.

## 5. The name

- Trimmed; not empty; no control characters; at most the length the request allows (section 2).
- The same name as now: nothing is sent.
- The same name as another sensor: as Garmin Connect allows (section 2); refused only if it refuses.

## 6. Errors, 429, what is kept

- One change request a click or a command, never sent again by itself: 401/403 – the usual words
  of signing in; 429 – Garmin's server asks to wait, said as for any request, no retry; no network –
  `applog.network_error_text`.
- After an error the kept list stays as it was: nothing is taken as changed. After a change the kept
  answers of the sensors are forgotten.
- The sensor's identifier (section 2; the serial number, if that is what the request needs) is read
  from a list asked for the change, held in memory for it only: never kept on disk, never shown,
  never logged. The kept answers of the sensors keep only the fields they keep now.

## 7. Log and privacy

- The log has one line for each request, as for every request: the host and the status, never a
  sensor's name, its identifier or the new name.
- `docs/privacy.md`: the new request, what it sends (the sensor's identifier and its new name) and
  that it changes the account.

## 8. Steps

Each step: tests that fail on the code before it, the texts with their Polish in the same commit,
the usual checks; each waits for acceptance.

1. The request learnt (D1), in a private note; this document completed (section 2, the limits of
   section 5).
2. The core and the command line: the request, the identifier from the list, the read-back, the
   kept answers forgotten; `openexpress sensors rename`; `docs/cli.md`, `docs/privacy.md`; tests on
   made-up answers.
3. The window: frames of the row and of the dialog in English and Polish at 360 and 900 px, for
   acceptance; then the row and the dialog.
4. The live trial (section 9), then the guide and README.

## 9. The live trial

Run by the user, never by the agent, on the branch of step 2, through `bash ~/oe`: rename one sensor
and give it its old name back.
- `openexpress sensors rename "Speed" "Speed test" --apply`: the list (1 request, unless kept), the
  change (1), the list read back (1);
- `openexpress sensors rename "Speed test" "Speed" --apply`: the change (1), the list read back (1)
  (the list just read back is kept);
- 5 requests in all; then Garmin Connect's phone app shows the old name again.

## 10. Decisions

| | Question | Recommendation | What the other way costs |
|---|---|---|---|
| D1 | How do we learn the request? | *Chosen:* (a) an analysis session on Garmin Connect for Android | (b) the web page: quicker, but it may not offer renaming, and shows only what the page sends, not the limits the app checks |
| D2 | What does one click to rename? | the sensor's row opens the dialog (edit icon at its end) | a Rename button on each row: five labelled buttons in a list, cramped at 360 px |
| D3 | How does the command line name the sensor? | by its whole name, as `openexpress sensors` prints it | by its place in that list: the order changes as sensors connect, so a place may name another sensor |
| D4 | Read the list back after a change? | yes, one request more | without it the window shows the name sent, not the one the account keeps |
| D5 | In which release? | the next one, after the trial of section 9 | in 0.12.0: a write to the account released untried |
