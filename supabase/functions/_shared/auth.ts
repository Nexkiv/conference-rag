import { createClient } from "jsr:@supabase/supabase-js@2.118.0";
import { json } from "./cors.ts";

/**
 * Manual authentication for Edge Functions.
 *
 * The functions are deployed with --no-verify-jwt, so nothing checks the
 * caller before this code runs. We ask Supabase Auth who the token belongs
 * to; a forged or expired token gets a 401.
 *
 * The client is built with the publishable key the browser sent in its
 * `apikey` header (supabase-js always sends it). The legacy anon key that
 * SUPABASE_ANON_KEY holds is disabled on this project.
 */
export async function requireAuth(req: Request): Promise<
    { user: { id: string; email?: string } } | { error: Response }
> {
    const token = req.headers.get("Authorization")?.replace(/^Bearer\s+/i, "");
    const apiKey = req.headers.get("apikey");
    if (!token || !apiKey) {
        return { error: json({ error: "Missing Authorization header" }, 401) };
    }

    const supabase = createClient(Deno.env.get("SUPABASE_URL") ?? "", apiKey);
    const { data: { user }, error } = await supabase.auth.getUser(token);

    if (error || !user) {
        return { error: json({ error: "Invalid or expired token" }, 401) };
    }
    return { user };
}
