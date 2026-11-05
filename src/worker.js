// Worker entrypoint. Kept to a single default export: workerd rejects any other non-handler named export.
import { createHandler } from "./auth.js";

export default { fetch: createHandler() };
