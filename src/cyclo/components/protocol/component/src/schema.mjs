export function servicesFromDescriptorSet(descriptorSet) {
  if (!descriptorSet || !Array.isArray(descriptorSet.file)) {
    throw new TypeError("schema is not a FileDescriptorSet JSON document");
  }

  const services = new Set();
  for (const file of descriptorSet.file) {
    if (!file || typeof file !== "object") {
      throw new TypeError("schema contains an invalid file descriptor");
    }
    const packageName = typeof file.package === "string" ? file.package : "";
    const fileServices = file.service ?? [];
    if (!Array.isArray(fileServices)) {
      throw new TypeError("schema contains an invalid service list");
    }
    for (const service of fileServices) {
      if (!service || typeof service.name !== "string" || !service.name) {
        throw new TypeError("schema contains an invalid service descriptor");
      }
      services.add(packageName ? `${packageName}.${service.name}` : service.name);
    }
  }
  return services;
}
