# Express User API

A basic CRUD (Create, Read, Update, Delete) User API built with Node.js and Express. Data is kept in an in-memory store, so no database setup is required to get running — swap in a real database later by editing `src/models/user.model.js` behind the same function signatures.

## Project structure

```
express-user-api/
├── config/
│   └── index.js          # Environment-based configuration (port, env, log level)
├── src/
│   ├── app.js             # Express app factory (middleware, routes, error handling)
│   ├── routes/
│   │   └── users.routes.js
│   ├── controllers/
│   │   └── users.controller.js
│   └── models/
│       └── user.model.js  # In-memory data store
├── tests/
│   └── users.test.js      # Jest + Supertest unit/integration tests
├── index.js                # Entry point — starts the HTTP server
├── package.json
├── .env.example
└── README.md
```

The app is split into an app factory (`src/app.js`) and a server entry point (`index.js`) so tests can exercise the Express app directly with Supertest, without binding to a real port.

## Requirements

- Node.js >= 18
- npm

## Setup

1. **Install dependencies**

   ```bash
   npm install
   ```

2. **Configure environment variables (optional)**

   Copy the example env file and adjust as needed:

   ```bash
   cp .env.example .env
   ```

   | Variable   | Default       | Description                          |
   |------------|---------------|---------------------------------------|
   | `PORT`     | `3000`        | Port the server listens on            |
   | `NODE_ENV` | `development` | `development`, `test`, or `production`|

   Configuration is centralized in `config/index.js`, which selects settings per `NODE_ENV`.

3. **Run the server**

   ```bash
   npm start
   ```

   For auto-restart on file changes during development:

   ```bash
   npm run dev
   ```

   The server starts on `http://localhost:3000` by default.

4. **Run the tests**

   ```bash
   npm test
   ```

## API Reference

Base path: `/api/users`

### Health check

```
GET /health
```

Returns `{ "status": "ok" }`.

### List all users

```
GET /api/users
```

**Response** `200 OK`
```json
[
  { "id": 1, "name": "Ada Lovelace", "email": "ada@example.com" }
]
```

### Get a single user

```
GET /api/users/:id
```

**Response** `200 OK` — the user object
**Response** `404 Not Found` — if no user with that id exists

### Create a user

```
POST /api/users
Content-Type: application/json

{ "name": "Ada Lovelace", "email": "ada@example.com" }
```

**Response** `201 Created` — the created user, including its assigned `id`
**Response** `400 Bad Request` — missing/invalid `name` or `email`
**Response** `409 Conflict` — a user with that email already exists

### Update a user

```
PUT /api/users/:id
Content-Type: application/json

{ "name": "Ada Byron" }
```

Partial updates are supported — omit fields you don't want to change.

**Response** `200 OK` — the updated user
**Response** `400 Bad Request` — invalid `name` or `email`
**Response** `404 Not Found` — if no user with that id exists
**Response** `409 Conflict` — if the new email is already used by another user

### Delete a user

```
DELETE /api/users/:id
```

**Response** `204 No Content` — on success
**Response** `404 Not Found` — if no user with that id exists

## Example usage (curl)

```bash
# Create a user
curl -X POST http://localhost:3000/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "Ada Lovelace", "email": "ada@example.com"}'

# List users
curl http://localhost:3000/api/users

# Get a single user
curl http://localhost:3000/api/users/1

# Update a user
curl -X PUT http://localhost:3000/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "Ada Byron"}'

# Delete a user
curl -X DELETE http://localhost:3000/api/users/1
```

## Testing

Tests use [Jest](https://jestjs.io/) and [Supertest](https://github.com/ladjs/supertest) to exercise the Express app in-process (no running server required). Each test resets the in-memory user store via `User.reset()` in a `beforeEach` hook to keep tests isolated from one another.

```bash
npm test
```

## Notes on extending this project

- **Persistence**: Replace the functions in `src/models/user.model.js` (`getAll`, `getById`, `create`, `update`, `remove`, `findByEmail`) with calls to a real database client (e.g. Postgres, MongoDB). Controllers and routes don't need to change since they only depend on that function signature.
- **Validation**: Field validation currently lives in the controller. For more complex schemas, consider a library like `zod` or `joi`.
- **Auth**: There is no authentication/authorization layer. Add middleware in `src/app.js` before mounting `usersRouter` if needed.
