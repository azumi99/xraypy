export function warehousePath(path, access) {
  const withoutWarehouse = path.replace(/^\/(cas|bgd)(?=\/|$)/, "");
  return `/${access}${withoutWarehouse && withoutWarehouse !== "/" ? withoutWarehouse : "/live"}`;
}
