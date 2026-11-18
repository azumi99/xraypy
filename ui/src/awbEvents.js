export function shouldRefreshAwbs(event) {
  return typeof event?.type === "string" && (event.type.startsWith("awb.") || event.type === "parcel.saved" || event.type === "parcel.review_resolved");
}
