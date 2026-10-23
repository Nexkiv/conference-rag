// CORS: the site (GitHub Pages) and the functions (supabase.co) are different
// origins, so the browser asks permission first with an OPTIONS request.
// "*" is fine here because auth uses a bearer token, not cookies.
export const corsHeaders = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
}

// Generic error for the browser; the details only go to the function logs.
export function serverError(err: unknown, message: string, status = 500): Response {
    const errorId = crypto.randomUUID().slice(0, 8);
    console.error(`[${errorId}]`, err);
    return json({ error: message, error_id: errorId }, status);
}

export const MAX_QUESTION_CHARS = 500;
