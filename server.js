// server.js
require('dotenv').config();
const express = require('express');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

const app = express();

// Serve static files (fresh.html, etc.) from the current directory
app.use(express.static(__dirname)); // <— Added here

// Parse JSON bodies
app.use(express.json());

// CORS for frontend (adjust origin in production)
app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*'); // Change to your domain later
    res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept');
    next();
});

// Create Stripe Checkout Session for subscription
app.post('/create-checkout-session', async (req, res) => {
    const { priceId } = req.body;

    if (!priceId) {
        return res.status(400).json({ error: 'Price ID is required' });
    }

    try {
        const session = await stripe.checkout.sessions.create({
            mode: 'subscription',
            payment_method_types: ['card'],
            line_items: [{ price: priceId, quantity: 1 }],
            success_url: `${req.headers.origin}/success.html?session_id={CHECKOUT_SESSION_ID}`,
            cancel_url: `${req.headers.origin}/cancel.html`,
        });

        res.json({ sessionId: session.id });
    } catch (error) {
        console.error('Stripe error:', error);
        res.status(500).json({ error: error.message });
    }
});

// Start the server
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log(`Server running on http://localhost:${PORT}`);
    console.log(`Open fresh.html: http://localhost:${PORT}/fresh.html`);
});