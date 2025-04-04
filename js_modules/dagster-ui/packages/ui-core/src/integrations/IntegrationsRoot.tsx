import {Redirect, Route, Switch} from 'react-router';

import {IntegrationRoot} from './IntegrationRoot';
import {MarketplaceRoot} from './MarketplaceRoot';
import {useFeatureFlags} from '../app/Flags';

export const IntegrationsRoot = () => {
  const {flagMarketplace} = useFeatureFlags();

  if (!flagMarketplace) {
    return <Redirect to="/deployment" />;
  }

  return (
    <Switch>
      <Route path="/integrations" component={MarketplaceRoot} exact />
      <Route path="/integrations/:integrationName" component={IntegrationRoot} />
    </Switch>
  );
};
