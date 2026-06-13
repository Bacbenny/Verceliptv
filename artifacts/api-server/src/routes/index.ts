import { Router, type IRouter } from "express";
import healthRouter from "./health";
import tieulamRelayRouter from "./tieulam-relay";

const router: IRouter = Router();

router.use(healthRouter);
router.use(tieulamRelayRouter);

export default router;
