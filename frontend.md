## Customer section

- Browse active produce and search for nearby farms by product, quantity, and location.
- View farm profiles, produce offers, harvest dates, quality grades, and farm photos.
- Build a basket, choose delivery location and time window, and place an order.
- View order status and delivery progress.
- Request substitutions where supported; show cancellation or issue reporting according to the platform’s policies.

## Farmer section

- View and manage their farm profile, land parcels, and produce offers.
- Upload farm and produce photos.
- Receive vendor commitment requests and confirm quantities, readiness times, shortfalls, or rejection.
- View quality check results and availability status.
- Track orders or pickups related to their produce.

The backend currently supports photo upload and operational quality checks, but it does not yet provide farmer login, offer-management endpoints, or farmer-specific authorization. Those need backend support before this section can be fully interactive.

## Driver section

- View offered assignments and accept or decline them.
- See assigned stops in order, with pickup and delivery details.
- Record arrival, pickup, delivery, failure, or quality-rejection events, with optional location and notes.
- View current route and assignment status.

The backend has assignment-response and stop-event endpoints. The frontend will also need a maps/navigation integration to show routes; the route planner currently receives road travel matrices from outside the backend.

## Platform Operations section

This section is central to your platform-maintainer perspective.

- Dashboard: orders by status, upcoming vendor commitments, route progress, and demand insights.
- Orders: inspect order details, advance lifecycle status, and handle orders needing review.
- Supply: search nearby farms, compare offers, request and review vendor commitments, and respond to shortfalls.
- Quality: record inspection outcomes and remove failed produce from availability.
- Dispatch: create planning jobs, monitor queued/running/succeeded/infeasible jobs, review routes, and allocate drivers.
- Demand and logistics: view the historical demand baseline and variability alongside supply commitments and route status.
- Demo data: generate seeded sample farms and offers for development or demonstrations.


## Vendor Section

This may add additional cost but may good to be have a middleman

 - Update the farms and products available to each and every farm.
 - Have to update the app every 1or2times/week.
