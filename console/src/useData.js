import { useCallback, useEffect, useState } from "react";
import { AuthError, api } from "./api.js";

// Load one API path; `reload()` fetches it again. A 401 sends the user back to sign-in.
export function useData(path, onAuthError) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    setError("");
    try {
      setData(await api(path));
    } catch (e) {
      if (e instanceof AuthError) onAuthError();
      else setError(e.message);
    }
  }, [path, onAuthError]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { data, error, reload };
}
