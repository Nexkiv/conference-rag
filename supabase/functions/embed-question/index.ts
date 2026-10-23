import "jsr:@supabase/functions-js@2.118.0/edge-runtime.d.ts";
import { corsHeaders, json, serverError } from "../_shared/cors.ts";
import { requireAuth } from "../_shared/auth.ts";
import { invalidQuestion } from "../_shared/validate.ts";
import { embedQuestion } from "../_shared/openai.ts";

Deno.serve(async (req) => {
    if (req.method === "OPTIONS") {
        return new Response("ok", { headers: corsHeaders });
    }

    const auth = await requireAuth(req);
    if ("error" in auth) return auth.error;

    try {
        const body = await req.json();

        // Health check from the page: proves deploy + login work, costs nothing.
        if (body.ping === true) return json({ ok: true });

        const bad = invalidQuestion(body.question);
        if (bad) return bad;

        return json({ embedding: await embedQuestion(body.question) });
    } catch (err) {
        return serverError(err, "Embedding failed");
    }
});
